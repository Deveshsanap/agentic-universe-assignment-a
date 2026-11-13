"""Tests for Assignment A. Run from the repo root:  pytest

Fixtures are tiny hand-built drops in tmp_path, using the real schemas
in assignment-a-data/schemas, so the tests check the actual rules
(phone normalisation, dates, dispositions, buckets, idempotency)
rather than a copy of them.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from solution import normalize as nz
from solution.ingest import ingest_drop
from solution.reports import derive, write_reports
from solution.store import connect, get_counter

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"


# ------------------------------------------------------------------ helpers

def _drop(tmp: Path, name: str = "drop_1") -> Path:
    drop = tmp / name
    (drop / "leads" / "motor_renewal").mkdir(parents=True)
    (drop / "events").mkdir(parents=True)
    return drop


def _write_leads(drop: Path, rows: list[dict], batch: str = "batch_001",
                 headers: list[str] | None = None) -> None:
    headers = headers or ["customer_name", "phone", "policy_number",
                          "policy_expiry", "premium_amount", "preferred_language"]
    canonical = ["customer_name", "phone", "policy_number",
                 "policy_expiry", "premium_amount", "preferred_language"]
    with open(drop / "leads" / "motor_renewal" / f"{batch}.csv",
              "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for row in rows:
            # Headers may be aliases (batch_003 style); map positionally.
            w.writerow({h: row[c] for h, c in zip(headers, canonical)})


def _write_events(drop: Path, lines: list[str], name: str = "events_0001.jsonl") -> None:
    with open(drop / "events" / name, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _lead(name="Asha Sharma", phone="+91 98765 43210", ref="MOT/2026/100001",
          expiry="2026-12-01", amount="10000", lang="en") -> dict:
    return {"customer_name": name, "phone": phone, "policy_number": ref,
            "policy_expiry": expiry, "premium_amount": amount,
            "preferred_language": lang}


def _event(eid, etype, call, when, phone="+919876543210", code=None,
           workflow="motor_renewal", override=False) -> str:
    event = {"event_id": eid, "event_type": etype, "workflow_id": workflow,
             "call_id": call, "occurred_at": when,
             "payload": {}}
    if not override:
        event["phone"] = phone
    if code is not None:
        event["payload"] = {"disposition_code": code}
    return json.dumps(event)


def _fresh_db(tmp: Path) -> sqlite3.Connection:
    return connect(tmp / "test.db")


# ------------------------------------------------------------------ normalize

@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("0 9876543210", "+919876543210"),
    ("91-98765-43210", "+919876543210"),
    ("(+91) 9876543210", "+919876543210"),
    ("987654321", None),          # 9 digits
    ("5876543210", None),         # bad first digit
    ("+1 415 555 0134", None),    # US number
    ("N/A", None),
])
def test_phone_normalisation(raw, expected):
    assert nz.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-dec-2026", "2026-12-01"),
    ("31/02/2026", None),   # impossible date
    ("2026-13-01", None),   # impossible month
    ("soon", None),
])
def test_date_parsing(raw, expected):
    assert nz.parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("10000", "10000.00"),
    ("₹12,345", "12345.00"),
    ("Rs. 5000", "5000.00"),
    ("INR 99.9", "99.90"),
    ("12.345", "12.35"),   # rounds to 2 places
    ("-5", None),
    ("N/A", None),
    ("twelve thousand", None),
])
def test_decimal_parsing(raw, expected):
    assert nz.parse_decimal(raw) == expected


def test_header_normalisation():
    assert nz.normalize_header("  Mobile_No ") == "mobile no"
    assert nz.normalize_header("Insured   Name") == "insured name"
    assert nz.normalize_header("customer_name") == "customer name"


def test_timestamp_forms():
    utc = nz.parse_timestamp("2026-10-05T13:20:21Z")
    assert utc is not None and nz.ist_date(utc) == "2026-10-05"
    # 01:20 UTC = 06:50 IST the same day; 20:00 UTC = 01:30 IST next day.
    assert nz.ist_date(nz.parse_timestamp("2026-10-05T20:00:00Z")) == "2026-10-06"
    naive = nz.parse_timestamp("2026-10-03 10:01:00")
    assert naive is not None and nz.ist_iso(naive) == "2026-10-03T10:01:00+05:30"


# ------------------------------------------------------------------ leads

def test_lead_ingest_alias_headers_and_rejections(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [
        _lead(),                                   # valid
        _lead(phone="5876543210"),                 # invalid phone
        _lead(name="   ", ref="MOT/2026/100002"),  # missing name
        _lead(ref="", phone="+91 91234 56789"),    # missing ref
        _lead(phone="+91 91234 56789", expiry="31/02/2026",
              amount="N/A", lang="Hindi"),         # valid, 3 field warnings
    ], headers=["Insured Name", "Mobile No", "Policy No", "Expiry Date",
                "Premium", "Language"])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    assert get_counter(conn, "lead_rows_total") == 5
    assert get_counter(conn, "lead_rows_rejected") == 3
    assert get_counter(conn, "field_warnings") == 3
    leads = conn.execute("SELECT * FROM leads").fetchall()
    assert len(leads) == 2
    # PII: no name or full phone anywhere in the leads table.
    dumped = json.dumps([dict(r) for r in leads])
    assert "Asha Sharma" not in dumped and "9876543210" not in dumped
    assert leads[0]["phone_masked"] == "+91******3210"
    rejections = conn.execute(
        "SELECT reason FROM rejections ORDER BY row_number").fetchall()
    assert [r["reason"] for r in rejections] == [
        "invalid_phone", "missing_required:customer_name",
        "missing_required:policy_number"]
    conn.close()


def test_duplicate_merging_and_defaults(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [
        _lead(expiry="2026-12-01", amount="10000", lang="hi"),
        # Same phone, later row: new premium wins, empty expiry does not
        # overwrite, language changes. Still one lead, batch_001 kept.
        _lead(expiry="", amount="₹12,000", lang="ta"),
    ])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    assert get_counter(conn, "duplicate_lead_rows_merged") == 1
    row = conn.execute("SELECT * FROM leads").fetchone()
    fields = json.loads(row["fields_json"])
    assert row["batch_id"] == "batch_001"
    assert fields["premium_amount"] == "12000.00"
    assert fields["policy_expiry"] == "2026-12-01"
    assert fields["preferred_language"] == "ta"
    # Default: a lead with no language at all reports as "en".
    drop2 = _drop(tmp_path, "drop_2")
    _write_leads(drop2, [_lead(phone="+91 90000 00001", lang="")],
                 batch="batch_003")
    ingest_drop(conn, drop2, SCHEMAS)
    derived = derive(conn, SCHEMAS)
    assert derived["lead_report.csv"][1]["external_ref"] == "MOT/2026/100001"
    conn.close()


def test_idempotent_reingest(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [_lead(), _lead(phone="+91 90000 00001")])
    _write_events(drop, [_event("evt_1", "call.initiated", "call_1",
                                "2026-10-05T10:00:00Z")])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    first = write_reports(conn, SCHEMAS, tmp_path / "reports1")
    ingest_drop(conn, drop, SCHEMAS)  # same drop again
    second = write_reports(conn, SCHEMAS, tmp_path / "reports2")
    assert first == second
    assert get_counter(conn, "lead_rows_total") == 2
    assert get_counter(conn, "events_total") == 1
    conn.close()


# ------------------------------------------------------------------ events

def test_orphan_event_counts_once_lead_arrives(tmp_path):
    drop = _drop(tmp_path)
    # Event first: its lead is not ingested yet, so it is an orphan.
    _write_events(drop, [_event("evt_1", "call.initiated", "call_1",
                                "2026-10-05T10:00:00Z")])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    assert derive(conn, SCHEMAS)["data_quality.json"]["orphan_events"] == 1
    # Now the lead arrives in a later drop: the same event must count.
    drop2 = _drop(tmp_path, "drop_2")
    _write_leads(drop2, [_lead()], batch="batch_003")
    ingest_drop(conn, drop2, SCHEMAS)
    derived = derive(conn, SCHEMAS)
    assert derived["data_quality.json"]["orphan_events"] == 0
    assert derived["lead_report.csv"][0]["total_calls"] == 1
    assert derived["lead_report.csv"][0]["bucket"] == "in_progress_dialed"
    conn.close()


def test_effective_disposition_override_wins_and_unknown_ignored(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [_lead()])
    _write_events(drop, [
        _event("evt_1", "call.initiated", "call_1", "2026-10-05T10:00:00Z"),
        _event("evt_2", "call.connected", "call_1", "2026-10-05T10:00:20Z"),
        _event("evt_3", "disposition.set", "call_1", "2026-10-05T10:02:00Z",
               code="callback_requested"),
        # Unknown code on a second call must be ignored, not crash anything.
        _event("evt_4", "call.initiated", "call_2", "2026-10-06T10:00:00Z"),
        _event("evt_5", "disposition.set", "call_2", "2026-10-06T10:01:00Z",
               code="RENEWD"),
        # Override (no phone, links via call_id) beats the earlier set.
        _event("evt_6", "disposition.override", "call_1",
               "2026-10-06T09:00:00Z", code="renewed", override=True),
        # Malformed line and a duplicate delivery.
        "{not json",
        _event("evt_1", "call.initiated", "call_1", "2026-10-05T10:00:00Z"),
    ])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    derived = derive(conn, SCHEMAS)
    quality = derived["data_quality.json"]
    assert quality["events_total"] == 8
    assert quality["malformed_events"] == 1
    assert quality["duplicate_events_dropped"] == 1
    assert quality["unknown_disposition_events"] == 1
    lead = derived["lead_report.csv"][0]
    assert lead["total_calls"] == 2
    assert lead["connected_calls"] == 1
    # Most recent call (call_2) has no known disposition; the current
    # disposition comes from the most recent call that HAS one: call_1,
    # whose override says "renewed" -> terminal + success.
    assert lead["current_disposition"] == "renewed"
    assert lead["ever_success"] == "true"
    assert lead["bucket"] == "completed"
    conn.close()


def test_bucket_telephony_failed(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [_lead(), _lead(phone="+91 90000 00001")])
    _write_events(drop, [
        _event("evt_1", "call.initiated", "call_1", "2026-10-05T10:00:00Z"),
        _event("evt_2", "call.telephony_failed", "call_1",
               "2026-10-05T10:00:05Z"),
    ])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    rows = derive(conn, SCHEMAS)["lead_report.csv"]
    assert rows[0]["bucket"] == "in_progress_telephony_failed"
    assert rows[1]["bucket"] == "yet_to_reach_out"
    conn.close()


def test_daily_overview_and_connectivity(tmp_path):
    drop = _drop(tmp_path)
    _write_leads(drop, [_lead()])
    _write_events(drop, [
        _event("evt_1", "call.initiated", "call_1", "2026-10-05T20:30:00Z"),
        # 20:30 UTC = 02:00 IST next day, so this call belongs to 2026-10-06.
        _event("evt_2", "call.connected", "call_1", "2026-10-05T20:30:20Z"),
        _event("evt_3", "call.initiated", "call_2", "2026-10-06T10:00:00Z"),
    ])
    conn = _fresh_db(tmp_path)
    ingest_drop(conn, drop, SCHEMAS)
    daily = derive(conn, SCHEMAS)["daily_overview.csv"]
    assert daily == [
        {"workflow_id": "motor_renewal", "date": "2026-10-06",
         "total_calls": 2, "connected_calls": 1,
         "connectivity_rate": "0.5000", "success_leads": 0},
    ]
    conn.close()
