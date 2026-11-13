# Assignment A — Interaction Ledger & Reporting Pipeline

Candidate: Devesh Sanap. See the root `SUBMISSION.md` for the design,
decisions, and results.

## What this does

Reads messy lead CSVs and call-event JSONL files ("drops"), validates and
normalises them, stores them in a SQLite database, and writes the five
report files the spec asks for.

```
solution/
├── pipeline.py     # CLI: `python -m solution.pipeline ingest ... / report ...`
├── ingest.py       # lead + event ingestion (validation, dedupe, idempotency)
├── normalize.py    # pure field rules: phone, date, decimal, enum, timestamps
├── reports.py      # linking, calls, buckets, and the five report files
├── store.py        # SQLite tables + counters
└── tests/
    └── test_pipeline.py
```

## How to run (from the repository root)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # only pytest is needed for this assignment
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/
```

Re-running `ingest` on a drop you already ingested changes nothing.
The database lives at `solution/ledger.db` by default; use `--db <path>`
to put it somewhere else. The `--db` / `--schemas` flags go *before*
the subcommand, e.g. `python -m solution.pipeline --db /tmp/x.db ingest data/drop_1`.

## How to test

```bash
python -m pytest  # 34 tests, <1s
```

Tests cover phone normalisation, dates, decimals, header matching,
rejection reasons, duplicate merging + defaults, effective disposition
(override beats set, unknown codes ignored), bucket logic, orphans that
arrive before their lead, and idempotent re-ingest.

## How it works, in one paragraph

Leads are validated against their workflow schema in schema order; the
first failure rejects the row (`missing_required:<field>` or
`invalid_phone`). Valid rows are merged by `(workflow_id, phone)` — the
first row fixes the `batch_id`, later rows overwrite only non-null
fields. Events go into an append-only `events` table keyed by `event_id`
(duplicates and malformed lines are counted and skipped). Nothing about
linking is decided at ingest time: at report time each event is linked
to a lead by phone hash, or — for overrides, which have no phone — by
`call_id`. That is why an event that arrives before its lead still
counts once the lead arrives. PII is never stored in plaintext: the
customer name is validated and then dropped, and the phone is stored
only as a SHA-256 hash (for matching) plus its masked form
`+91******1234` (for the report).
