# Submission

## Candidate
- **Name:** Devesh Sanap
- **Assignment chosen:** A (Data) — Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** ~4–5 hours of focused work (including reading the spec and generator, building, testing, and verifying on seed 42)

## How to run
```bash
# From a clean clone, in the repository root:
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # only pytest is needed for this assignment
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports/

python -m pytest                      # 34 tests, <1s
```
The database lives at `solution/ledger.db` by default (`--db <path>` moves
it; the flag goes before the subcommand). Re-running `ingest` on a drop
already ingested changes nothing. Generated `data/`, `reports/` and
`*.db` are gitignored and are not part of the submission.

Note on submission: the repo README asks for read access to
`[[REVIEWER_HANDLES]]` and an email to `[[SUBMISSION_EMAIL]]`, but both
placeholders are unfilled in the materials I received. The operative
route in the email I received is the Microsoft Form, which I will use
to submit the private repo link.

## Design

Three stages, one direction: **ingest -> SQLite -> report**.

```
CSV / JSONL drops ──> ingest.py ──> SQLite (leads, events, rejections,
                      ingested_files, counters) ──> reports.py ──> 5 files
```

- **Ingest is dumb and strict.** It validates each lead row against its
  workflow schema (in schema order, first failure wins), merges
  duplicates, and appends events to the ledger. It decides nothing
  about what a report should say.
- **The database is the single source of truth.** Reports are derived
  from it every time, so re-running `report` after a new drop always
  reflects everything ingested so far.
- **Linking happens at report time, not ingest time.** That is the one
  design choice the whole assignment turns on: an event can arrive
  before its lead, so an event stored as "unlinked" today may be
  linked tomorrow. If I had decided linking at ingest time, I would
  have had to either drop those events (wrong) or re-process old drops
  (fragile). Storing every non-malformed, non-duplicate event and
  re-linking on every report is simpler and always correct.

Database schema (SQLite, see `solution/store.py`):

| Table | Key columns | Why |
|---|---|---|
| `leads` | PK `(workflow_id, phone_hash)` | One row per lead identity. `phone_hash` is the SHA-256 of the canonical phone; `phone_masked` (`+91******1234`) is stored for the report; the name is validated and then dropped — it is never stored at all. Other fields live in `fields_json` (non-PII only). `batch_id` is fixed by the first valid row. |
| `events` | PK `event_id` | Append-only ledger. Never updated or deleted — an override is a new row. `phone_hash` (nullable), `occurred_at` (ISO), `payload_json`. |
| `rejections` | PK `(workflow_id, batch_id, row_number)` | Rejected lead rows with their single reason. No PII. |
| `ingested_files` | PK `(drop_name, rel_path, sha256)` | What has already been processed — this is what makes re-ingest a no-op. |
| `counters` | PK `key` | Totals that only make sense at ingest time (rows seen, duplicates merged, warnings, malformed/duplicate event lines). Orphan and unknown-disposition counts are **not** stored here; they are computed at report time from the final state. |

Linking, in order: (1) a non-override event links by
`(workflow_id, phone_hash)`; (2) a `disposition.override` (no phone)
links through its `call_id` to the lead of that call's other events;
(3) any other phone-less event with a known `call_id` links the same
way. A call exists only if a linked `call.initiated` is present; its
date is the IST date of the earliest initiation. Effective disposition
is the latest override by `occurred_at`, else the latest set, among
disposition events whose code is in the catalogue — unknown codes are
counted in `data_quality.json` and ignored for the disposition.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Link events at report time, store orphans | Events can precede their lead; orphan counts must reflect the final state | Decide at ingest + keep an orphan queue to re-process (more moving parts, easy to lose events) |
| PII: drop the name, hash the phone, store only the masked form | No report needs the name; the phone is needed only to match events and to print the last 4 digits | Reversible encryption with a key in `.env` (more code, key management, and still a secret to protect; hashing is enough for matching) |
| Idempotency by (drop, file, content hash) in `ingested_files` | Re-running the same drop is a no-op even if files are re-read; duplicates are also caught by the `event_id` PK and lead-merge logic | Track only drop names (would silently skip a drop whose files changed) |
| Defaults applied at report time, not ingest time | "Defaults apply after merging, to fields still null" — merging is only finished when all drops are in, so applying them earlier could be overwritten wrongly | Apply at insert (simpler, but a later duplicate providing a real value would then have to beat a default) |
| A present-but-invalid value on a *required* non-phone field counts as a warning and stores null, not a rejection | The spec defines only two rejection reasons (`missing_required:*`, `invalid_phone`); inventing a third reason would break the diff. This case cannot occur with the given schemas (their required fields are string/phone only) | Reject with a made-up reason |
| `disposition.*` events with a missing `disposition_code` count as unknown | A disposition with no code cannot be matched to the catalogue | Treat as having no disposition (would hide a data problem) |
| Decimal rounding is half-up via `Decimal` | `round()` on floats (banker's rounding + binary float error) would give wrong cents on values like 12.345 | `float` + `round()` |
| No SQL aggregation for reports; Python derives them from the DB | The logic (effective disposition, buckets, orphan re-linking) is easier to read and test in Python at this scale; the DB is still the store of record | Do aggregation in SQL (a stretch goal; see below) |

## Failure modes considered

- **Dirty input:** bad phones, missing required fields, impossible dates, bad currency/enum values — rejected or warned per the rules, never crashing the run.
- **Duplicate delivery:** duplicate lead rows merge (last non-null wins); duplicate `event_id`s (even across drops) are counted and skipped; the `event_id` PK makes a double-insert impossible.
- **Re-runs:** re-ingesting a drop is a byte-identical no-op (verified — see Results).
- **Events before leads / out-of-order events:** stored and re-linked at report time; disposition ordering uses `occurred_at`, not file order.
- **Lost/unknown dispositions:** unknown codes are counted and ignored rather than failing the run; a call with only an unknown disposition has no effective disposition.
- **Crash mid-ingest:** each file is committed after it is processed, so a crash loses at most the file in progress, and re-running ingest picks it up (its hash is not yet recorded).
- **Not handled:** concurrent writers (two processes ingesting at once) — SQLite would serialize/lock, and this is a single-user pipeline; a corrupt/partially-written input file is treated as whatever rows parse, not validated as a whole; the large-scale (140k leads) performance stretch was not run (see below).

## Testing

- **34 pytest tests, all passing** (`python -m pytest`, <1s), in `solution/tests/test_pipeline.py`.
- Covered: phone normalisation (9 formats incl. invalid), all four date formats + impossible dates, decimals (₹/Rs/INR, commas, rounding, negatives), header alias matching, rejection reasons + row numbering + blank rows, duplicate merging (non-null overwrite, batch_id kept) + defaults, effective disposition (override beats set, unknown code ignored), bucket logic (incl. all-telephony-failed), orphans that arrive before their lead, daily overview IST-date handling (a 20:30 UTC call belongs to the next IST day), and idempotent re-ingest (reports identical).
- **Not tested:** the `--scale large` dataset (timing only — the code path is identical, but I did not measure it); a mid-ingest crash (the per-file commit design is untested by an actual kill); Postgres (SQLite was chosen, and the SQL used is plain enough to port).

## Results

Seed 42, small scale. Timings on this machine: ingest drop_1 ~1.3s,
ingest drop_2 ~0.6s, report ~0.3s (total ~2.2s).

`data_quality.json`:
```json
{
  "lead_rows_total": 2057,
  "lead_rows_rejected": 108,
  "rejections_by_reason": {
    "invalid_phone": 42,
    "missing_required:customer_name": 25,
    "missing_required:order_id": 13,
    "missing_required:phone": 14,
    "missing_required:policy_number": 14
  },
  "duplicate_lead_rows_merged": 57,
  "field_warnings": 122,
  "events_total": 8745,
  "malformed_events": 8,
  "duplicate_events_dropped": 237,
  "orphan_events": 462,
  "unknown_disposition_events": 18
}
```

Verification performed (all passed):
- **Reconciliation against the raw files** (independent script): lead rows 2057, event lines 8745, malformed 8, duplicate event_ids 237 — all match `data_quality.json`; 2057 − 108 rejected − 57 merged = 1892 leads in `lead_report.csv`; batch bucket columns sum to `total_leads` per batch; daily `total_calls`/`connected_calls` equal the lead-report totals; every `connectivity_rate` is exactly 4 decimals and arithmetically correct; `rejections.csv` (108 rows) matches `rejections_by_reason`; every `phone_masked` matches `+91******dddd`; the DB holds 8500 stored events (8745 − 8 − 237) and no plaintext name or phone.
- **Idempotency:** re-ingesting drop_1 and drop_2 reported 8 files already ingested and skipped, and all five report files were byte-identical (md5).
- **Fresh database:** ingesting both drops into a new DB produced byte-identical reports to the sequential run.

Something surprising in the data: 462 orphan events is a lot, but it is expected — rejected leads (108) still receive calls, bad-phone leads receive calls on numbers that can never match, and the generator adds calls to numbers that were never uploaded. The orphan count is a data-quality signal, not a pipeline bug: those events are stored and would count the moment a matching lead appears.

## If I had another day

1. Run the `--scale large` dataset and report timings; if ingest is slow, batch the inserts (executemany) and add an index on `(workflow_id, call_id)`.
2. Move the report aggregation into SQL (the spec's stretch goal) and compare the outputs — the Python derivation is the reference.
3. Add a `--dry-run` / validation mode that reports what *would* be rejected without writing to the DB.
4. Add a production note: scheduling (daily drops), alerting on orphan/unknown rates, and a backfill procedure for re-processing a corrected drop (today a corrected file with a new hash would be ingested as new rows — I would add an explicit `--force` re-ingest that rebuilds from scratch instead).
