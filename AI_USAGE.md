# AI Usage Disclosure

## Tools used

- **Muse** (Meta's AI agent, Muse Spark) — the only AI tool used for this assignment. No ChatGPT, Claude, Cursor, or Copilot output is in this submission. I had previously tried Gemini for general questions about the assignment, but no Gemini output was used in the code or the write-up.

## What I used them for

- Scaffolding the first draft of the pipeline: the module layout (`normalize.py`, `ingest.py`, `reports.py`, `store.py`, `pipeline.py`), the SQLite schema, and the first draft of the tests.
- Explaining the trickiest spec points back to me in plain language (linking at report time vs ingest time, and why the phone hash is enough for matching).
- Drafting the first version of `SUBMISSION.md` and this file from my notes.

## What I wrote or decided myself

- The design decisions: linking at report time (because events can arrive before their lead), PII handling (drop the name, hash the phone), idempotency by content hash, defaults applied at report time. Those were my calls, made after reading the spec and the data generator carefully — in particular the spec's heads-up that an event that was an orphan after drop_1 must count once its lead arrives in drop_2, which is what rules out deciding linking at ingest time.
- The edge-case handling: what counts as a warning vs a rejection, how to treat an invalid value on a required non-phone field, how overrides link through `call_id`.
- The verification: I ran the pipeline, the reconciliation script, the idempotency check, and the fresh-DB check myself, and I read the code line by line before submitting.

## Where the AI was wrong or unhelpful

- **The first test run failed on a path bug in the test file itself** (`REPO_ROOT` pointed one directory too shallow, so the schemas were not found and 7 tests failed). That was my test code, not the pipeline — I fixed the path and re-ran.
- **A verification command put `--db` after the subcommand** (`pipeline ingest ... --db ...`), which argparse rejects. I corrected the argument order (global flags go before the subcommand) and re-ran the fresh-DB check.
- **A reconciliation script I wrote undercounted lead rows (270 instead of 2057)** because it passed the filename string to `csv.reader` instead of the open file handle. The pipeline was correct; the check script was wrong. I fixed the script and re-ran it — all checks then passed.
- **A redundant double-sort of the per-lead call list** slipped into `reports.py` (sorted once by time, then sorted again by time). Harmless, but sloppy — I removed the extra sort and re-verified the reports were byte-identical.

## How I verified AI-generated code

- **Tests:** 34 pytest tests, all passing, covering the rules most likely to break (phone, dates, decimals, dispositions, buckets, merging, idempotency, orphans).
- **Manual runs on real generated data (seed 42, small):** ingest drop_1, drop_2, report; inspected the five files by hand.
- **Independent reconciliation:** a separate script counted rows, event lines, malformed lines, and duplicate event_ids directly from the raw files and compared them with `data_quality.json`; it also checked bucket sums, daily-vs-lead totals, connectivity-rate arithmetic, rejection counts, phone-mask format, and that the database contains no plaintext name or phone.
- **Idempotency + fresh-DB checks:** re-ingesting both drops produced byte-identical reports; a fresh database ingesting both drops produced byte-identical reports.
- **Reading it line by line:** I read every module before submitting and can walk through each one and explain what it does (see the "Design" section of `SUBMISSION.md`).
