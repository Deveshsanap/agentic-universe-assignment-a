"""Ingest leads and events into the SQLite store.

Lead rules (from assignment-a-data/README.md):
- Headers are matched after normalising (lower-case, trim, runs of _ or
  whitespace -> one space) against the field name or its aliases.
- Blank rows are skipped and not counted; row numbers count non-blank
  data rows from 1.
- Fields are validated in schema order. The first failure is the row's
  single rejection reason: missing_required:<field> or invalid_phone.
- An optional field that is present but invalid stores null and adds one
  field warning (only for rows that are not rejected).
- Lead identity is (workflow_id, canonical phone). The first valid row
  creates the lead and fixes its batch_id; later valid rows count as one
  merged duplicate each and overwrite only fields that are non-null.

Event rules:
- Per line, in order: malformed (not valid JSON, or no event_id) -> count
  and skip; duplicate event_id (any drop) -> count and skip; anything
  else is stored in the append-only ledger, even if it cannot be linked
  yet — linking is decided at report time, so an event that arrives
  before its lead counts once the lead arrives.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from . import normalize as nz
from .store import add_counter


# ---------------------------------------------------------------- schemas

def load_schemas(schemas_dir: str | Path) -> dict:
    """Load every <workflow>.json schema, keyed by workflow_id."""
    schemas = {}
    for path in sorted(Path(schemas_dir).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
        schemas[schema["workflow_id"]] = schema
    return schemas


def load_dispositions(schemas_dir: str | Path) -> dict:
    """Load dispositions.csv -> {(workflow_id, code): {is_terminal, is_success}}."""
    catalogue = {}
    with open(Path(schemas_dir) / "dispositions.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            catalogue[(row["workflow_id"], row["code"])] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return catalogue


# ---------------------------------------------------------------- lead rows

def _validate_row(schema: dict, raw: dict) -> tuple[dict | None, str | None, int]:
    """Validate one lead row.

    Returns (clean_values, rejection_reason, field_warnings).
    clean_values maps field name -> normalised value (or None). PII fields
    (name, phone) are validated here but the caller drops them before
    storing; the phone's canonical form is returned under "phone" so the
    caller can hash/mask it.
    """
    values: dict = {}
    warnings = 0
    for field in schema["fields"]:
        name = field["name"]
        raw_value = raw.get(name)
        missing = raw_value is None or raw_value == ""
        if missing:
            if field.get("required"):
                return None, f"missing_required:{name}", 0
            values[name] = None
            continue
        ftype = field["type"]
        if ftype == "string":
            values[name] = raw_value
        elif ftype == "phone":
            canonical = nz.normalize_phone(raw_value)
            if canonical is None:
                return None, "invalid_phone", 0
            values[name] = canonical
        elif ftype == "date":
            parsed = nz.parse_date(raw_value)
            if parsed is None:
                # The spec only defines rejection for a missing required
                # field or an invalid phone. A present-but-invalid date on
                # an optional field is a warning (this is the only case
                # the generator produces); on a required field we do the
                # same rather than invent a new rejection reason.
                values[name] = None
                warnings += 1
            else:
                values[name] = parsed
        elif ftype == "decimal":
            parsed = nz.parse_decimal(raw_value)
            if parsed is None:
                values[name] = None
                warnings += 1
            else:
                values[name] = parsed
        elif ftype == "enum":
            parsed = nz.parse_enum(raw_value, field.get("values", []))
            if parsed is None:
                values[name] = None
                warnings += 1
            else:
                values[name] = parsed
        else:  # unknown type in a future schema: keep the raw value
            values[name] = raw_value
    return values, None, warnings


def _ingest_lead_file(conn: sqlite3.Connection, path: Path, schemas: dict) -> None:
    workflow_id = path.parent.name
    schema = schemas.get(workflow_id)
    if schema is None:
        print(f"  skip {path}: no schema for workflow '{workflow_id}'")
        return
    batch_id = path.stem  # e.g. batch_001 — fixed permanently for new leads

    # Map each CSV column to a schema field name (or None = ignore column).
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    header_map: dict[int, str | None] = {}
    lookup = {}
    for field in schema["fields"]:
        lookup[nz.normalize_header(field["name"])] = field["name"]
        for alias in field.get("aliases", []):
            lookup[nz.normalize_header(alias)] = field["name"]
    for col, header in enumerate(rows[0]):
        header_map[col] = lookup.get(nz.normalize_header(header))

    reference_field = next(
        (f["name"] for f in schema["fields"] if f.get("reference")), None
    )
    pii_fields = {f["name"] for f in schema["fields"] if f.get("pii")}

    row_number = 0
    for raw_row in rows[1:]:
        if all(cell.strip() == "" for cell in raw_row):
            continue  # blank row: skipped and not counted
        row_number += 1
        add_counter(conn, "lead_rows_total")
        raw: dict[str, str | None] = {}
        for col, field_name in header_map.items():
            if field_name is None:
                continue
            value = raw_row[col].strip() if col < len(raw_row) else ""
            raw[field_name] = value if value != "" else None

        values, reason, warnings = _validate_row(schema, raw)
        if reason is not None:
            add_counter(conn, "lead_rows_rejected")
            conn.execute(
                "INSERT OR IGNORE INTO rejections "
                "(workflow_id, batch_id, row_number, reason) VALUES (?, ?, ?, ?)",
                (workflow_id, batch_id, row_number, reason),
            )
            continue
        add_counter(conn, "field_warnings", warnings)

        canonical_phone = values["phone"]
        key = nz.phone_hash(canonical_phone)
        masked = nz.phone_masked(canonical_phone)
        # PII never reaches the database: drop the name, keep only the
        # hash + masked form of the phone.
        stored = {k: v for k, v in values.items() if k not in pii_fields}
        external_ref = values.get(reference_field) or ""

        existing = conn.execute(
            "SELECT fields_json, external_ref FROM leads "
            "WHERE workflow_id = ? AND phone_hash = ?",
            (workflow_id, key),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO leads (workflow_id, phone_hash, phone_masked, "
                "batch_id, external_ref, fields_json) VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, key, masked, batch_id, external_ref,
                 json.dumps(stored, sort_keys=True)),
            )
        else:
            # Duplicate: overwrite only the fields where the new row has
            # a non-null value; batch_id stays with the first row.
            merged = json.loads(existing["fields_json"])
            for field_name, value in stored.items():
                if value is not None:
                    merged[field_name] = value
            new_ref = external_ref or existing["external_ref"]
            conn.execute(
                "UPDATE leads SET fields_json = ?, external_ref = ? "
                "WHERE workflow_id = ? AND phone_hash = ?",
                (json.dumps(merged, sort_keys=True), new_ref, workflow_id, key),
            )
            add_counter(conn, "duplicate_lead_rows_merged")


# ---------------------------------------------------------------- events

def _ingest_event_file(conn: sqlite3.Connection, path: Path) -> None:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip() == "":
                continue  # empty lines are not counted at all
            add_counter(conn, "events_total")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                add_counter(conn, "malformed_events")
                continue
            if not isinstance(event, dict) or not event.get("event_id"):
                add_counter(conn, "malformed_events")
                continue
            event_id = event["event_id"]
            exists = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if exists:
                add_counter(conn, "duplicate_events_dropped")
                continue
            phone = event.get("phone")
            phone_hash = None
            if phone:
                canonical = nz.normalize_phone(str(phone))
                if canonical is not None:
                    phone_hash = nz.phone_hash(canonical)
            occurred = nz.parse_timestamp(event.get("occurred_at"))
            conn.execute(
                "INSERT INTO events (event_id, workflow_id, call_id, event_type, "
                "phone_hash, occurred_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event.get("workflow_id") or "",
                    event.get("call_id"),
                    event.get("event_type") or "",
                    phone_hash,
                    occurred.isoformat() if occurred else None,
                    json.dumps(event.get("payload") or {}, sort_keys=True),
                ),
            )


# ---------------------------------------------------------------- drop

def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ingest_drop(conn: sqlite3.Connection, drop_dir: str | Path,
                schemas_dir: str | Path) -> dict:
    """Ingest one drop directory. Files already ingested (same drop, same
    relative path, same content hash) are skipped, so re-running ingest
    on a drop changes nothing."""
    drop = Path(drop_dir)
    drop_name = drop.name
    schemas = load_schemas(schemas_dir)
    stats = {"lead_files": 0, "event_files": 0, "skipped_files": 0}

    files = sorted(drop.rglob("*.csv")) + sorted(drop.rglob("*.jsonl"))
    for path in files:
        rel = str(path.relative_to(drop))
        digest = _file_sha256(path)
        seen = conn.execute(
            "SELECT 1 FROM ingested_files "
            "WHERE drop_name = ? AND rel_path = ? AND sha256 = ?",
            (drop_name, rel, digest),
        ).fetchone()
        if seen:
            stats["skipped_files"] += 1
            continue
        if path.suffix == ".csv":
            _ingest_lead_file(conn, path, schemas)
            stats["lead_files"] += 1
        else:
            _ingest_event_file(conn, path)
            stats["event_files"] += 1
        conn.execute(
            "INSERT INTO ingested_files (drop_name, rel_path, sha256) "
            "VALUES (?, ?, ?)",
            (drop_name, rel, digest),
        )
        conn.commit()
    return stats
