# Submission

## Candidate
- **Name:** Gyana Ranjan Sahoo
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~3h of active work, almost all of it in a Claude Code session
  that wrote and verified the code (see `AI_USAGE.md`)

## How to run
```bash
# from a clean clone, Python 3.11+
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/

pytest                                   # 79 tests

# everything at once gives identical reports:
#   python -m solution --db all.sqlite ingest data && python -m solution --db all.sqlite report --out reports_all
```
Re-running any `ingest` is a no-op. To start over, delete `ledger.sqlite*`. More detail in
[`solution/README.md`](solution/README.md).

## Design

**Core idea: store facts, derive interpretations.** Some outcomes are permanent the moment a
line is read: *malformed*, *duplicate event_id*, *lead row rejected*. Those are decided at
ingest. Others depend on what else has been ingested: *is this event an orphan?*, *what is this
call's effective disposition?*, *which bucket is this lead in?* Those are never stored. They are
recomputed in SQL from the ledger every time `report` runs. That is what makes "an orphan
after drop_1 counts once its lead arrives in drop_2" work, and it is why ingesting drop by drop
and ingesting everything at once give byte-identical reports.

```
 CSV ──► validate/normalise ──► lead_rejection (no PII)
                    │
                    └──────────► lead (merged projection) ◄── lead_row (audit: created/merged)
                                    ▲  join on (workflow_id, HMAC(phone))
 JSONL ─► malformed? dup? ─► ledger_event (append-only, narrow) + ledger_event_detail (append-only)
                                    │
                              report time (SQL, temp tables):
                              link events → calls → effective disposition → lead buckets → 5 files
 source_file: one row per ingested file + its counters  (idempotency + data_quality totals)
```

**Schema** (`solution/schema.sql`):

| table | role | key constraints |
|---|---|---|
| `source_file` | one row per ingested file, with its counters | unique `(workflow_id, batch_id)` for lead files, unique `sha256` for event files |
| `lead` | merged lead projection | unique `(workflow_id, phone_index)` = lead identity; `batch_id` set once on insert |
| `lead_row` | which accepted CSV row created / merged into which lead | PK `(file_id, row_number)` |
| `lead_rejection` | rejected rows: workflow, batch, row number, reason. No PII | PK `(workflow_id, batch_id, row_number)` |
| `ledger_event` | the interaction ledger: typed, normalised fields of every first-seen, well-formed event | `event_id` unique; `UPDATE`/`DELETE` blocked by triggers |
| `ledger_event_detail` | the rest of the original event (raw timestamp, encrypted raw phone, payload, unknown keys) | 1:1 with `ledger_event`, also append-only |
| `disposition` | catalogue, refreshed from `dispositions.csv` each run | PK `(workflow_id, code)` |

The ledger is split into two tables so the columns every report scans stay narrow. With one
wide table, the large dataset took 275 s to ingest and the report did not finish in 10 minutes;
after the split (plus the affinity fix below) it is 20 s + 5 s.

**Atomicity / idempotency.** Each file is ingested in one transaction together with its
`source_file` row, so a crash leaves the file either fully in or not in at all. Re-running then
picks it up. A lead batch is identified by `(workflow_id, batch_id)`. Re-ingesting the same
content is skipped. Different content under an already-used batch name is refused with an
error, because batches are immutable and silently merging a second `batch_001` would corrupt
row numbers and batch ownership. Event files are identified by content hash. Even without that,
event_id dedupe would keep the ledger correct, but the *counters* (duplicates, malformed, total
lines) would double.

**PII at rest** (`solution/pii.py`). One 32-byte master key, kept outside the DB, derives two
sub-keys:
- `phone_index = HMAC-SHA256(k1, "+91XXXXXXXXXX")`: a deterministic *blind index*, used
  to match events to leads with a plain indexed equality join. A bare SHA-256 would be worthless,
  because there are only ~4×10⁹ valid Indian mobiles and that space can be brute-forced in
  minutes. The HMAC can't be reversed without the key.
- AES-256-GCM with a random nonce for the recoverable values: lead phone, name (all `pii: true`
  fields), and the raw phone string on each event.
- `phone_last4` is stored in plaintext so reports can produce the mask; four digits identify no
  one.
- The DB stores a key fingerprint. Running with a different key refuses to start instead of
  silently turning every event into an orphan.
- Malformed lines are counted but never stored, since they may contain PII we can't parse out.

**Reports in SQL** (stretch). `report.py` builds temp tables in memory:
`direct_event` (phone link) → `call_lead` (call → lead, used to link overrides) →
`linked_event` → `call_fact` (connected, telephony_failed, effective disposition) →
`lead_fact` (bucket etc.). Then come five SELECTs. Python only formats (`connectivity_rate`,
booleans, masking).

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Linking (orphan status) is derived at report time, never stored | Orphan status changes when later leads arrive. Storing it would need back-filling and could drift | Store `lead_id` on the event and re-link orphans on each ingest (faster reports, mutable ledger) |
| `disposition.override` links **only** via `(workflow_id, call_id)`; other events **only** via phone | Literal reading of the spec | Fall back to call_id for phone-less non-override events |
| If a call's phone-linked events point at different leads, overrides follow the lead of `call.initiated`, else the lowest lead_id | Deterministic. Never happens in generated data | Treat as orphan |
| A call belongs to the lead its `call.initiated` links to; `connected`/`telephony_failed`/dispositions only count if linked to the same lead | Keeps a call's facts consistent | Group by call_id alone |
| Effective disposition ignores unknown codes; ties on `occurred_at` broken by `event_id` | Unknown codes are "ignored" per spec. The tie-break is order-independent, so drop order can't change results | Tie-break by ingest order |
| An event with missing/unparseable `occurred_at` counts as **malformed** | It can't be dated, ordered or reported. Never occurs in generated data | Store with NULL time and exclude from time-based facts |
| `event_id` must be a non-empty string or integer; any other JSON type, or non-object JSON, is malformed | "has no event_id" | |
| Whitespace-only lines are not counted in `events_total` | "every non-empty line". The trailing newline should not count | Count them as malformed |
| Phone strip uses ASCII digits only | `\d` would accept Devanagari/Arabic-Indic digits | |
| Required field present but unparseable (not phone) → `invalid_value:<field>` | The spec only defines reasons for missing/invalid-phone. Can't happen with the given schemas (required fields are strings) | Treat as warning (would create a lead without its reference) |
| Two columns mapping to the same field → first non-empty value in column order | Deterministic, keeps data | First column only |
| Dates accept 1- or 2-digit day/month (`1/2/2026`) | Same format, lenient. Generator always pads | Strict 2 digits |
| Decimals: commas are stripped anywhere (`1,00,000` is valid) | Indian grouping is common in this data's domain | Validate western grouping |
| Decimal rounding is ROUND_HALF_UP | Money. Python's `round` is banker's rounding. Not visible in any report | |
| `connectivity_rate` uses Python `f"{x:.4f}"` | Most likely how the expected output is produced | Decimal half-up (differs only on exact ties like 1/32) |
| Defaults are written when a lead is created rather than "after merging" | Provably equivalent: a default is only replaced by a later non-null value, exactly as last-non-null-wins merging would do | Store raw and apply on read |
| A merged duplicate may also change `external_ref` / name (non-null overwrite applies to all fields) | Literal spec. Identity is the phone | Freeze the reference field |
| `batch_summary` lists every ingested lead batch, even one with 0 leads | A batch that's all rejects/duplicates is still a batch someone uploaded | Only batches with leads (identical on generated data) |
| `rejections_by_reason` lists only reasons that occurred | No canonical list of reasons. On generated data every reason occurs anyway | List all possible reasons with 0 |
| `external_ref` stored in plaintext | It's needed in reports and isn't marked PII | |
| SQLite, not Postgres | Zero setup for reviewers. The design (constraints, triggers, SQL reports) ports directly | Postgres via docker-compose |

## Failure modes considered
- **Re-running ingest** on any drop: no-op (file-level identity, transactional).
- **Crash mid-file**: the transaction rolls back and the next run ingests the file from scratch.
  Files already committed are skipped.
- **Same batch name, different content**: refused with an error (exit code 1). Other files
  still ingest.
- **Events before their lead**: kept in the ledger and linked at report time.
- **Duplicate events** in a file, across files, across drops: first occurrence wins, the rest
  are counted.
- **Malformed lines** (truncated JSON, non-objects, missing id/time): counted, not stored.
- **Overrides** for calls we never saw: orphans. Overrides never modify earlier events:
  triggers abort any `UPDATE`/`DELETE` on the ledger.
- **Wrong / lost PII key**: refused via fingerprint check (exit code 2), never silent
  mis-linking. **Lost key = phone index unusable.** In production the key lives in a KMS with
  backups and rotation (re-index job).
- **Unknown workflow directory** (no schema): skipped with an error, exit code 1.
- **CSV quirks**: BOM, `\r\n`, short rows, extra columns, blank rows, renamed/re-ordered
  headers.

**Not handled / known limits**
- Catalogue changes are applied retroactively (reports always use the current
  `dispositions.csv`). That's arguably right, but there's no history of catalogue versions.
- Schema changes to existing workflows aren't versioned. A changed schema applies to new files
  only.
- One writer at a time (SQLite `BEGIN IMMEDIATE`). Concurrent ingests serialise rather than fail.
- An event whose `phone` is valid but whose `workflow_id` is wrong stays an orphan. We don't
  guess.
- No key rotation tooling.

## Testing
`pytest` runs 79 tests in under a second:
- **Rules most likely to break** (`test_normalize.py`, `test_rows.py`): every phone format the
  generator emits, invalid phones (landline, +1, 9/13 digits, non-ASCII digits), every date
  format, impossible dates, currency formats, header normalisation, the three timestamp forms
  and the IST-midnight boundary, rejection order, warnings vs rejections, blank-row handling.
- **End to end** (`test_pipeline.py`): a 2-drop dataset whose expected five reports are worked
  out by hand. It covers override beating a later `disposition.set`, unknown codes, all four
  buckets, `current_disposition` falling back to an earlier call, a call dated on the next IST
  day, a lead arriving one drop after its events, cross-drop duplicate events. Also tested:
  re-ingest no-op, incremental == all-at-once, no plaintext PII bytes in the DB file, wrong-key
  refusal, append-only triggers, batch-conflict refusal.
- **Mutation check**: removing "override beats set" from the SQL makes the end-to-end test
  fail.
- **Cross-check against an independent reference (not committed)**: during development I wrote
  a naive in-memory re-implementation straight from the README (no DB, no SQL) and diffed its
  five files against the pipeline. They are byte-identical on seeds 42, 7, 1234, 2026, 5 and 99
  (small) and seed 42 (large). For every seed, reports were also identical between drop-by-drop
  ingest with repeated re-ingests and all-at-once ingest. Caveat: the reference shares my
  reading of the spec, so it catches implementation bugs, not interpretation differences.

Not tested: concurrent writers, very large single files (memory: one event file is buffered
before its transaction, ~5k lines each here).

## Results
Seed 42, small (2,057 lead rows, 8,745 event lines): ingest ≈ 0.2 s total, report ≈ 0.05 s.

Seed 42, large (143,990 lead rows, 632,602 event lines), MacBook, SQLite file DB:

| step | time |
|---|---|
| `ingest data_large` (both drops) | **20.1 s** |
| `report` | **5.1 s** |

Two performance bugs found on the way, both now fixed:
1. Wide ledger rows (the full JSON body in the hot table): ingest took 275 s and was mostly
   I/O. Fixed by splitting off `ledger_event_detail`.
2. Report > 10 min: temp-table columns built from `CASE`/`MIN()` had **no type affinity**, so
   SQLite couldn't use the index when joining them to `lead.lead_id` (INTEGER) and fell back to
   an O(leads × calls) scan. Fixed with `CAST(... AS INTEGER)`. `EXPLAIN QUERY PLAN` showed
   `SCAN c` instead of `SEARCH c USING INDEX`.

Things in the data worth knowing (seed 42 small): 108 of 2,057 rows rejected (42 invalid
phones). 57 duplicate rows merged, including duplicates *inside the same file*. 122 field
warnings. 237 duplicate events (some cross drops). 462 orphan events in the final state. 18
events with codes not in the catalogue (`RENEWD`, `call_back`, `unknown`). Some events for
rejected leads (missing name/ref) end up as orphans, which is correct but worth flagging to ops
as "calls made to leads we consider invalid". Also, two values in the README examples
(`0.5625` for motor_renewal on 2026-10-01, and `2026-10-14T17:55:24+05:30` for
`MOT/2026/100000`) match this pipeline's seed-42 output exactly.

## Running this in production at 100k leads/day (stretch note)
- **Storage**: Postgres. Partition `ledger_event` by month on `occurred_at`. Same constraints and
  triggers (or revoke `UPDATE`/`DELETE` grants from the app role).
- **Scheduling**: drops land in object storage. An orchestrator (Airflow/Temporal/cron) runs
  `ingest <drop>` on arrival, then `report`. Since ingest is idempotent per file, retries are
  always safe, so "at-least-once" triggering is enough.
- **Incremental reports**: at 100k/day a full recompute is fine for months, since it is linear
  and takes seconds. When it isn't, keep linking derived but materialise `call_fact`/`lead_fact`
  per lead, and recompute only leads touched since the last run (new events, new leads, or a
  lead whose phone matches a previous orphan).
- **Backfills**: a re-sent or corrected file gets a new batch name, or an explicit
  `--supersede` operation that writes a new version rather than editing in place. A catalogue or
  logic change is a pure report re-run, because reports are a function of the ledger.
- **Failures / observability**: per-file rows in `source_file` are the run log. Alert on
  rejection rate, orphan rate and malformed rate jumping versus the trailing average. Those
  three caught every injected defect here.
- **Keys**: master key in KMS, envelope encryption, separate index key so encryption keys can
  rotate without re-indexing.

## If I had another day
1. Ask the data owners the open questions above (zero-lead batches, reason list, rounding tie)
   and lock them in with tests.
2. Postgres backend behind the same SQL (mostly dialect-compatible already) plus a docker
   based test run.
3. Touched-lead incremental reporting for the "100k/day for a year" case.
4. A `doctor` command: per-batch rejection/orphan rates and the top rejection reasons, for ops.
5. Key rotation (`reindex` with a new index key) and property-based tests (Hypothesis) for
   the phone/date/decimal parsers.
