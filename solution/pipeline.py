"""Command line for Assignment A.

Run from the repository root:

    python -m solution.pipeline ingest data/drop_1
    python -m solution.pipeline ingest data/drop_2
    python -m solution.pipeline report --out reports/

The database lives at solution/ledger.db by default (use --db to move
it). Re-running ingest on a drop you already ingested changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root = the folder that contains solution/, assignment-a-data/, ...
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB = REPO_ROOT / "solution" / "ledger.db"


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="solution.pipeline",
        description="Agentic Universe Assignment A: lead/event pipeline.")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="SQLite database file (default: solution/ledger.db)")
    parser.add_argument("--schemas", default=str(DEFAULT_SCHEMAS),
                        help="Directory with the workflow schemas + dispositions.csv")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Ingest one drop directory.")
    ingest.add_argument("drop", help="Drop directory, e.g. data/drop_1")

    report = sub.add_parser("report", help="Write the five report files.")
    report.add_argument("--out", required=True, help="Output directory")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    from .ingest import ingest_drop
    from .reports import write_reports
    from .store import connect

    args = _parse_args(argv)
    conn = connect(args.db)
    try:
        if args.command == "ingest":
            stats = ingest_drop(conn, args.drop, args.schemas)
            print(f"Ingested {args.drop}: "
                  f"{stats['lead_files']} lead file(s), "
                  f"{stats['event_files']} event file(s), "
                  f"{stats['skipped_files']} file(s) already ingested, skipped.")
        else:
            derived = write_reports(conn, args.schemas, args.out)
            quality = derived["data_quality.json"]
            print(f"Wrote 5 report files to {args.out}/")
            print(f"  leads: {len(derived['lead_report.csv'])} rows, "
                  f"{quality['lead_rows_rejected']} rejected, "
                  f"{quality['duplicate_lead_rows_merged']} duplicates merged")
            print(f"  events: {quality['events_total']} lines, "
                  f"{quality['malformed_events']} malformed, "
                  f"{quality['duplicate_events_dropped']} duplicates, "
                  f"{quality['orphan_events']} orphans, "
                  f"{quality['unknown_disposition_events']} unknown dispositions")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
