"""Command line entry point.

    python -m solution ingest data/drop_1 [data/drop_2 ...]
    python -m solution ingest data              # every drop_* inside, in natural order
    python -m solution report --out reports/
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from .db import KeyMismatchError, connect, sync_dispositions
from .ingest_events import ingest_event_file
from .ingest_leads import BatchConflictError, ingest_lead_file
from .report import build_reports
from .schemas import DEFAULT_SCHEMA_DIR, load_dispositions, load_schemas

DEFAULT_DB = Path("ledger.sqlite")


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def resolve_drops(paths: list[Path]) -> list[Path]:
    """Each arg is a drop dir (has leads/ or events/) or a parent of drop dirs."""
    drops: list[Path] = []
    for p in paths:
        if not p.is_dir():
            raise SystemExit(f"error: {p} is not a directory")
        if (p / "leads").is_dir() or (p / "events").is_dir():
            drops.append(p)
            continue
        children = sorted((c for c in p.iterdir()
                           if c.is_dir() and ((c / "leads").is_dir() or (c / "events").is_dir())),
                          key=_natural_key)
        if not children:
            raise SystemExit(f"error: {p} contains no drops (expected leads/ or events/)")
        drops.extend(children)
    return drops


def cmd_ingest(args) -> int:
    schemas = load_schemas(args.schemas)
    conn, codec = connect(args.db, args.key_file)
    sync_dispositions(conn, load_dispositions(args.schemas))
    seen_ids = {r[0] for r in conn.execute("SELECT event_id FROM ledger_event")}
    errors = 0
    t0 = time.perf_counter()

    for drop in resolve_drops(args.paths):
        print(f"drop {drop}")
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(d for d in leads_dir.iterdir() if d.is_dir()):
                schema = schemas.get(wf_dir.name)
                files = sorted(wf_dir.glob("*.csv"), key=lambda p: p.name)
                if schema is None:
                    print(f"  ERROR no schema for workflow {wf_dir.name!r}; skipped {len(files)} file(s)",
                          file=sys.stderr)
                    errors += 1
                    continue
                for f in files:
                    try:
                        o = ingest_lead_file(conn, codec, schema, f)
                    except BatchConflictError as e:
                        print(f"  ERROR {e}", file=sys.stderr)
                        errors += 1
                        continue
                    if o.skipped:
                        print(f"  leads  {f}: already ingested, skipped")
                    else:
                        print(f"  leads  {f}: rows={o.rows_total} rejected={o.rows_rejected} "
                              f"merged={o.duplicates_merged} warnings={o.field_warnings} new_leads={o.leads_created}")
        events_dir = drop / "events"
        if events_dir.is_dir():
            for f in sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name):
                o = ingest_event_file(conn, codec, f, seen_ids)
                if o.skipped:
                    print(f"  events {f}: already ingested, skipped")
                else:
                    print(f"  events {f}: lines={o.lines_total} malformed={o.malformed} "
                          f"duplicates={o.duplicates} appended={o.appended}")
    print(f"ingest finished in {time.perf_counter() - t0:.2f}s")
    return 1 if errors else 0


def cmd_report(args) -> int:
    conn, _ = connect(args.db, args.key_file)
    sync_dispositions(conn, load_dispositions(args.schemas))
    t0 = time.perf_counter()
    q = build_reports(conn, args.out)
    print(f"reports written to {args.out}/ in {time.perf_counter() - t0:.2f}s")
    print(f"  leads rows={q['lead_rows_total']} rejected={q['lead_rows_rejected']} | "
          f"events={q['events_total']} orphans={q['orphan_events']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m solution", description="Interaction ledger pipeline")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database (default: %(default)s)")
    ap.add_argument("--key-file", type=Path, default=None,
                    help="PII key file (default: <db>.key; AU_PII_KEY env var overrides)")
    ap.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMA_DIR,
                    help="directory with <workflow>.json and dispositions.csv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest one or more drops")
    p.add_argument("paths", nargs="+", type=Path)
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("report", help="write the five report files")
    p.add_argument("--out", type=Path, default=Path("reports"))
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyMismatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
