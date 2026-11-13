# AI Usage Disclosure

## Tools used
- Claude Code (Anthropic, Claude Opus model), run in my terminal on this repository.

## What I used them for
Claude Code did most of the implementation from the assignment README:
- Chose Assignment A and designed the schema (append-only ledger, file-level idempotency,
  report-time linking, HMAC blind index + AES-GCM for PII).
- Wrote all the code in `solution/` and the 79 tests in `solution/tests/`.
- Wrote a separate throwaway reference implementation (not committed) and diffed its output
  against the pipeline on 6 seeds plus the large dataset.
- Profiled and fixed the two performance problems described in `SUBMISSION.md`.
- Drafted `SUBMISSION.md`.

## What I wrote or decided myself
Honestly, very little of the technical work. I gave Claude Code the assignment email and asked
it to complete the task end to end. The choice of assignment, the design, the code, the tests and
the decisions in `SUBMISSION.md` came from the AI. My part was directing the session, approving
its actions (including creating this private repository and pushing to it), and deciding what to
submit. I am studying the code and the Decisions table in `SUBMISSION.md` so that I can explain
and extend it in the interview.

## Where the AI was wrong or unhelpful
- **First version was far too slow at scale.** The first report SQL ran for more than 10 minutes
  on the large dataset. The cause was temp-table columns built from `CASE`/`MIN()` expressions,
  which have no type affinity, so SQLite couldn't use the index on the join and scanned
  O(leads × calls). Found with `EXPLAIN QUERY PLAN` (`SCAN c` instead of `SEARCH ... USING
  INDEX`) and fixed with `CAST(... AS INTEGER)`. Report time went from >10 min to 5 s.
- **Ledger table was too wide.** The first design stored the whole JSON body in the main ledger
  table. Ingest of the large set took 275 s, mostly I/O wait (user CPU was only 33 s). It was
  split into a narrow `ledger_event` plus `ledger_event_detail`, bringing ingest to 20 s.
- **Small script bug**: a cross-check shell script used `$seed_$scale`, which zsh parses as
  the variable `seed_`. It was caught when the output directory name came out wrong.
- All three problems above were found by the AI's own profiling and checks, not by me.

## How I verified AI-generated code
- `pytest`: 79 tests, including an end-to-end dataset whose expected reports were worked out by
  hand, not produced by the code.
- Mutation check: deliberately broke "override beats disposition.set" in the SQL and confirmed
  the end-to-end test fails.
- An independent naive re-implementation, diffed byte-for-byte on seeds 42, 7, 1234, 2026, 5,
  99 and the large dataset. Incremental vs all-at-once ingest was also diffed for each seed.
- Manual runs of the documented commands from a clean state, including re-ingesting a drop.
- All of the verification above was run by Claude Code in the session. I have not done a
  separate line-by-line review of my own yet.
