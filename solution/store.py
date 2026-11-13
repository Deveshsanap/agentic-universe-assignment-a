"""SQLite storage for Assignment A.

Tables
------
leads
    One row per lead, keyed by (workflow_id, phone_hash). The phone number
    itself is NEVER stored: only its SHA-256 hash (for matching events) and
    its masked form (for the report). The customer's name is validated and
    then dropped entirely — no report needs it. All other lead fields live
    in fields_json as a small JSON object (non-PII fields only).
events
    The append-only interaction ledger. One row per event_id, never updated
    or deleted — an override is a new row, never an edit. Events that
    cannot be linked yet (orphans) are still stored here, so they can be
    linked later when their lead arrives; linking is decided at report
    time, not at ingest time.
rejections
    One row per rejected lead row, with its reason. No PII in here.
ingested_files
    (drop, relative path, sha256) of every file already processed, so
    re-running ingest on the same drop changes nothing.
counters
    Running totals that only make sense at ingest time: lead rows seen,
    duplicates merged, field warnings, event lines seen, malformed lines,
    duplicate events dropped. Orphan/unknown-disposition counts are NOT
    stored here — they are computed at report time from the final state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    workflow_id TEXT NOT NULL,
    phone_hash  TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    PRIMARY KEY (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    call_id     TEXT,
    event_type  TEXT NOT NULL,
    phone_hash  TEXT,
    occurred_at TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS ingested_files (
    drop_name TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    PRIMARY KEY (drop_name, rel_path, sha256)
);

CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the SQLite database (creating it if needed) and its tables."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_counter(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM counters WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else 0


def add_counter(conn: sqlite3.Connection, key: str, delta: int = 1) -> None:
    conn.execute(
        "INSERT INTO counters (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
        (key, delta),
    )
