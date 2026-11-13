"""Derive the five report files from the SQLite store.

Linking is decided here, at report time — never at ingest time. That is
what makes events that arrive before their lead count once the lead
arrives, and what makes orphan counts in data_quality.json reflect the
final state:

1. A non-override event links by (workflow_id, phone hash).
2. A disposition.override has no phone, so it links through its call_id
   to the lead of that call's other events.
3. Any other event with no phone but a known call_id links the same way.

Calls are grouped by (workflow_id, call_id) and only count if a
call.initiated event is present and linked. A call's effective
disposition is its latest override by occurred_at, else its latest set;
disposition events whose code is not in the catalogue are ignored for
this (and counted as unknown_disposition_events in data_quality.json).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import normalize as nz
from .ingest import load_dispositions, load_schemas
from .store import get_counter


# ---------------------------------------------------------------- loading

def _load_leads(conn: sqlite3.Connection, schemas: dict) -> list[dict]:
    """Leads with schema defaults applied to fields that are still null."""
    leads = []
    for row in conn.execute("SELECT * FROM leads"):
        schema = schemas.get(row["workflow_id"])
        fields = json.loads(row["fields_json"])
        if schema:
            for field in schema["fields"]:
                if fields.get(field["name"]) is None and "default" in field:
                    fields[field["name"]] = field["default"]
        leads.append({
            "workflow_id": row["workflow_id"],
            "phone_hash": row["phone_hash"],
            "phone_masked": row["phone_masked"],
            "batch_id": row["batch_id"],
            "external_ref": row["external_ref"],
            "fields": fields,
        })
    return leads


def _load_events(conn: sqlite3.Connection) -> list[dict]:
    events = []
    for row in conn.execute("SELECT * FROM events"):
        occurred = None
        if row["occurred_at"]:
            occurred = datetime.fromisoformat(row["occurred_at"])
        events.append({
            "event_id": row["event_id"],
            "workflow_id": row["workflow_id"],
            "call_id": row["call_id"],
            "event_type": row["event_type"],
            "phone_hash": row["phone_hash"],
            "occurred_at": occurred,
            "payload": json.loads(row["payload_json"]),
        })
    return events


# ---------------------------------------------------------------- linking

def link_events(leads: list[dict], events: list[dict]):
    """Return (lead_by_event_id, orphan_count, unknown_disposition_count).

    lead_by_event_id maps each stored event to its lead dict, or None if
    the event cannot be linked (orphan).
    """
    by_key = {(lead["workflow_id"], lead["phone_hash"]): lead for lead in leads}
    linked: dict[str, dict | None] = {}
    call_lead: dict[tuple, dict] = {}

    # Pass 1: events that carry a phone link directly.
    for event in events:
        if event["event_type"] == "disposition.override":
            continue
        key = (event["workflow_id"], event["phone_hash"])
        lead = by_key.get(key) if event["phone_hash"] else None
        linked[event["event_id"]] = lead
        if lead is not None and event["call_id"]:
            call_lead[(event["workflow_id"], event["call_id"])] = lead

    # Pass 2: overrides (no phone) link through their call_id.
    for event in events:
        if event["event_type"] != "disposition.override":
            continue
        lead = call_lead.get((event["workflow_id"], event["call_id"]))
        linked[event["event_id"]] = lead

    # Pass 3: anything else with no phone but a known call_id.
    for event in events:
        if linked.get(event["event_id"]) is None and event["call_id"]:
            linked[event["event_id"]] = call_lead.get(
                (event["workflow_id"], event["call_id"]))

    return linked, call_lead


# ---------------------------------------------------------------- derivation

def derive(conn: sqlite3.Connection, schemas_dir: str | Path) -> dict:
    """Compute all five reports in memory. Returns a dict of file name ->
    rows (list of dicts, or the data_quality dict)."""
    schemas = load_schemas(schemas_dir)
    catalogue = load_dispositions(schemas_dir)
    leads = _load_leads(conn, schemas)
    events = _load_events(conn)
    linked, _ = link_events(leads, events)

    orphan_events = sum(1 for e in events if linked[e["event_id"]] is None)
    unknown_dispositions = 0
    for event in events:
        if linked[event["event_id"]] is None:
            continue  # orphans are counted as orphans, not as unknown
        if event["event_type"].startswith("disposition."):
            code = event["payload"].get("disposition_code")
            if (event["workflow_id"], code) not in catalogue:
                unknown_dispositions += 1

    # Group linked events into calls: (workflow_id, call_id) -> events.
    # A call only exists if a call.initiated event is present.
    calls_by_lead: dict[tuple, list[dict]] = {}
    calls_by_key: dict[tuple, dict] = {}
    for event in events:
        lead = linked[event["event_id"]]
        if lead is None or not event["call_id"]:
            continue
        key = (event["workflow_id"], event["call_id"])
        calls_by_key.setdefault(key, {"lead": lead, "events": []})
        calls_by_key[key]["events"].append(event)
    for key, call in calls_by_key.items():
        initiated = [e for e in call["events"] if e["event_type"] == "call.initiated"
                     and e["occurred_at"] is not None]
        if not initiated:
            continue
        initiated.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
        first = initiated[0]
        call["initiated_at"] = first["occurred_at"]
        call["date"] = nz.ist_date(first["occurred_at"])
        call["connected"] = any(e["event_type"] == "call.connected"
                                for e in call["events"])
        call["telephony_failed"] = any(
            e["event_type"] == "call.telephony_failed" for e in call["events"])
        # Effective disposition: latest override, else latest set, among
        # disposition events whose code is in the catalogue.
        known = [e for e in call["events"]
                 if e["event_type"] in ("disposition.set", "disposition.override")
                 and e["occurred_at"] is not None
                 and (call["lead"]["workflow_id"],
                      e["payload"].get("disposition_code")) in catalogue]
        known.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
        overrides = [e for e in known if e["event_type"] == "disposition.override"]
        sets = [e for e in known if e["event_type"] == "disposition.set"]
        chosen = overrides[-1] if overrides else (sets[-1] if sets else None)
        call["disposition"] = (
            chosen["payload"].get("disposition_code") if chosen else None)
        lead_key = (call["lead"]["workflow_id"], call["lead"]["phone_hash"])
        calls_by_lead.setdefault(lead_key, []).append(call)

    def bucket(lead_calls: list[dict]) -> str:
        if not lead_calls:
            return "yet_to_reach_out"
        if any(c["disposition"] and catalogue[
                (c["lead"]["workflow_id"], c["disposition"])]["is_terminal"]
                for c in lead_calls):
            return "completed"
        if all(c["telephony_failed"] for c in lead_calls):
            return "in_progress_telephony_failed"
        return "in_progress_dialed"

    # --- lead_report.csv + batch_summary.csv
    lead_rows = []
    batch_counts: dict[tuple, dict] = {}
    for lead in sorted(leads, key=lambda l: (l["workflow_id"], l["external_ref"])):
        key = (lead["workflow_id"], lead["phone_hash"])
        lead_calls = sorted(calls_by_lead.get(key, []),
                            key=lambda c: c["initiated_at"])
        b = bucket(lead_calls)
        total = len(lead_calls)
        connected = sum(1 for c in lead_calls if c["connected"])
        last_call = lead_calls[-1]["initiated_at"] if lead_calls else None
        with_disp = [c for c in lead_calls if c["disposition"]]
        current = with_disp[-1]["disposition"] if with_disp else ""
        ever_success = any(
            c["disposition"] and catalogue[
                (lead["workflow_id"], c["disposition"])]["is_success"]
            for c in lead_calls)
        lead_rows.append({
            "workflow_id": lead["workflow_id"],
            "external_ref": lead["external_ref"],
            "batch_id": lead["batch_id"],
            "phone_masked": lead["phone_masked"],
            "bucket": b,
            "total_calls": total,
            "connected_calls": connected,
            "last_call_at": nz.ist_iso(last_call) if last_call else "",
            "current_disposition": current,
            "ever_success": "true" if ever_success else "false",
        })
        slot = batch_counts.setdefault(
            (lead["workflow_id"], lead["batch_id"]),
            {"total_leads": 0, "yet_to_reach_out": 0,
             "in_progress_telephony_failed": 0, "in_progress_dialed": 0,
             "completed": 0})
        slot["total_leads"] += 1
        slot[b] += 1

    batch_rows = [
        {"workflow_id": wf, "batch_id": batch, **counts}
        for (wf, batch), counts in sorted(batch_counts.items())
    ]

    # --- daily_overview.csv
    daily: dict[tuple, dict] = {}
    for key, call in calls_by_key.items():
        if "initiated_at" not in call:
            continue
        slot = daily.setdefault(
            (call["lead"]["workflow_id"], call["date"]),
            {"total_calls": 0, "connected_calls": 0, "success_leads": set()})
        slot["total_calls"] += 1
        if call["connected"]:
            slot["connected_calls"] += 1
        if call["disposition"] and catalogue[
                (call["lead"]["workflow_id"], call["disposition"])]["is_success"]:
            slot["success_leads"].add(
                (call["lead"]["workflow_id"], call["lead"]["phone_hash"]))
    daily_rows = []
    for (wf, date), slot in sorted(daily.items()):
        total = slot["total_calls"]
        daily_rows.append({
            "workflow_id": wf,
            "date": date,
            "total_calls": total,
            "connected_calls": slot["connected_calls"],
            "connectivity_rate": f"{slot['connected_calls'] / total:.4f}",
            "success_leads": len(slot["success_leads"]),
        })

    # --- rejections.csv
    rejection_rows = [
        {"workflow_id": r["workflow_id"], "batch_id": r["batch_id"],
         "row_number": r["row_number"], "reason": r["reason"]}
        for r in conn.execute(
            "SELECT * FROM rejections ORDER BY workflow_id, batch_id, row_number")
    ]

    # --- data_quality.json
    reasons: dict[str, int] = {}
    for r in rejection_rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    quality = {
        "lead_rows_total": get_counter(conn, "lead_rows_total"),
        "lead_rows_rejected": get_counter(conn, "lead_rows_rejected"),
        "rejections_by_reason": dict(sorted(reasons.items())),
        "duplicate_lead_rows_merged": get_counter(conn, "duplicate_lead_rows_merged"),
        "field_warnings": get_counter(conn, "field_warnings"),
        "events_total": get_counter(conn, "events_total"),
        "malformed_events": get_counter(conn, "malformed_events"),
        "duplicate_events_dropped": get_counter(conn, "duplicate_events_dropped"),
        "orphan_events": orphan_events,
        "unknown_disposition_events": unknown_dispositions,
    }

    return {
        "batch_summary.csv": batch_rows,
        "daily_overview.csv": daily_rows,
        "lead_report.csv": lead_rows,
        "rejections.csv": rejection_rows,
        "data_quality.json": quality,
    }


# ---------------------------------------------------------------- writing

_COLUMNS = {
    "batch_summary.csv": ["workflow_id", "batch_id", "total_leads",
                          "yet_to_reach_out", "in_progress_telephony_failed",
                          "in_progress_dialed", "completed"],
    "daily_overview.csv": ["workflow_id", "date", "total_calls",
                           "connected_calls", "connectivity_rate", "success_leads"],
    "lead_report.csv": ["workflow_id", "external_ref", "batch_id", "phone_masked",
                        "bucket", "total_calls", "connected_calls", "last_call_at",
                        "current_disposition", "ever_success"],
    "rejections.csv": ["workflow_id", "batch_id", "row_number", "reason"],
}


def write_reports(conn: sqlite3.Connection, schemas_dir: str | Path,
                  out_dir: str | Path) -> dict:
    """Write all five report files into out_dir. Returns the derived data."""
    derived = derive(conn, schemas_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, columns in _COLUMNS.items():
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for row in derived[name]:
                writer.writerow(row)
    with open(out / "data_quality.json", "w", encoding="utf-8") as f:
        json.dump(derived["data_quality.json"], f, indent=2)
        f.write("\n")
    return derived
