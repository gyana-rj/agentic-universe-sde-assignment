# Interaction Ledger & Reporting Pipeline (Assignment A)

Python 3.11+, SQLite, one third-party dependency (`cryptography`, for AES-GCM).

```bash
# from the repo root
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/

pytest            # 79 tests, < 1 s
```

Options (global, before the sub-command):

| flag | default | meaning |
|---|---|---|
| `--db PATH` | `ledger.sqlite` | SQLite database |
| `--key-file PATH` | `<db>.key` | PII master key; created on first run. `AU_PII_KEY` (base64, 32 bytes) overrides it |
| `--schemas DIR` | `assignment-a-data/schemas` | `<workflow>.json` + `dispositions.csv` |

`ingest` takes one or more drop directories, or a parent directory (every `drop_*` inside is
ingested in natural order). Re-ingesting anything is a no-op. Start from scratch by deleting
`ledger.sqlite*`.

## Layout

| file | what |
|---|---|
| `normalize.py` | pure value rules: header, phone, date, decimal, enum, timestamps |
| `schemas.py` | loads lead schemas and the disposition catalogue |
| `pii.py` | blind index (HMAC) + AES-GCM, key loading |
| `schema.sql` / `db.py` | database schema, append-only triggers, key-fingerprint check |
| `ingest_leads.py` | CSV → validate → reject / create / merge, one transaction per file |
| `ingest_events.py` | JSONL → malformed / duplicate → ledger, one transaction per file |
| `report.py` | linking + facts + the five reports, all in SQL |
| `cli.py` | command line |
| `tests/` | unit tests for the rules, end-to-end test with hand-computed expected output |

Design and decisions are in [`../SUBMISSION.md`](../SUBMISSION.md).
