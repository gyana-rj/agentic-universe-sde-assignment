"""Event JSONL ingestion into the append-only ledger.

Only steps 1 and 2 of the spec (malformed, duplicate) are decided here,
because they are permanent facts about a line. Steps 3 and 4 (orphan,
unknown disposition) depend on which leads exist, which changes as later
drops arrive, so they are derived at report time from the ledger.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .normalize import normalize_phone, parse_timestamp, to_ist_iso, IST
from .pii import PiiCodec

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class ParsedEvent:
    event_id: str
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at: datetime  # aware, UTC
    occurred_raw: str
    phone_raw: str | None
    disposition_code: str | None
    payload: object        # payload exactly as received
    extra: dict            # unexpected top-level keys


def parse_line(line: str) -> ParsedEvent | None:
    """Return the parsed event, or None if the line is malformed.

    Malformed = not valid JSON, not a JSON object, no usable event_id, or no
    parseable occurred_at (an event we can't place in time can't be reported).
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    eid = obj.get("event_id")
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or not str(eid).strip():
        return None
    occurred = parse_timestamp(obj.get("occurred_at"))
    if occurred is None:
        return None

    etype = _opt_str(obj.get("event_type"))
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    code = _opt_str(payload.get("disposition_code")) if etype and etype.startswith("disposition.") else None
    phone = obj.get("phone")
    return ParsedEvent(
        event_id=str(eid).strip(), event_type=etype, workflow_id=_opt_str(obj.get("workflow_id")),
        call_id=_opt_str(obj.get("call_id")), occurred_at=occurred,
        occurred_raw=obj["occurred_at"], phone_raw=None if phone is None else str(phone),
        disposition_code=code, payload=obj.get("payload", {}),
        extra={k: v for k, v in obj.items() if k not in _KNOWN_KEYS})


_KNOWN_KEYS = {"event_id", "event_type", "workflow_id", "call_id", "occurred_at", "phone", "payload"}


def _opt_str(v) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s or None


@dataclass
class EventFileOutcome:
    path: Path
    skipped: bool = False
    lines_total: int = 0
    malformed: int = 0
    duplicates: int = 0
    appended: int = 0


def ingest_event_file(conn: sqlite3.Connection, codec: PiiCodec, path: Path,
                      seen_ids: set[str]) -> EventFileOutcome:
    """Append one JSONL file to the ledger in a single transaction.

    ``seen_ids`` is the set of event_ids already in the ledger; it is updated
    in place only after a successful commit.
    """
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    out = EventFileOutcome(path)
    if conn.execute("SELECT 1 FROM source_file WHERE kind='events' AND sha256=?", (sha,)).fetchone():
        out.skipped = True
        return out

    rows, details = [], []
    new_ids: set[str] = set()
    for line_no, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue  # empty lines are not events and not counted
        out.lines_total += 1
        ev = parse_line(line)
        if ev is None:
            out.malformed += 1
            continue
        if ev.event_id in seen_ids or ev.event_id in new_ids:
            out.duplicates += 1
            continue
        new_ids.add(ev.event_id)
        canonical = normalize_phone(ev.phone_raw) if ev.phone_raw is not None else None
        ist = ev.occurred_at.astimezone(IST)
        delta = ev.occurred_at - _EPOCH
        rows.append((
            ev.event_id, line_no, ev.event_type, ev.workflow_id, ev.call_id,
            (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds,
            to_ist_iso(ev.occurred_at), ist.date().isoformat(),
            codec.blind_index(canonical) if canonical else None,
            ev.disposition_code,
        ))
        details.append((
            str(ev.occurred_raw),
            codec.encrypt(ev.phone_raw) if ev.phone_raw is not None else None,
            json.dumps(ev.payload, ensure_ascii=False, sort_keys=True),
            json.dumps(ev.extra, ensure_ascii=False, sort_keys=True) if ev.extra else None,
        ))
    out.appended = len(rows)

    conn.execute("BEGIN IMMEDIATE")
    try:
        file_id = conn.execute(
            "INSERT INTO source_file (kind, path, sha256, ingested_at, lines_total, malformed, duplicates) "
            "VALUES ('events', ?, ?, ?, ?, ?, ?)",
            (str(path), sha, datetime.now(timezone.utc).isoformat(),
             out.lines_total, out.malformed, out.duplicates)).lastrowid
        first_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger_event").fetchone()[0]
        conn.executemany(
            "INSERT INTO ledger_event (seq, event_id, file_id, line_no, event_type, workflow_id, call_id, "
            "occurred_at_us, occurred_ist, occurred_ist_date, phone_index, disposition_code) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(first_seq + i, r[0], file_id, *r[1:]) for i, r in enumerate(rows)])
        conn.executemany(
            "INSERT INTO ledger_event_detail (seq, occurred_at_raw, phone_enc, payload_json, extra_json) "
            "VALUES (?,?,?,?,?)",
            [(first_seq + i, *d) for i, d in enumerate(details)])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    seen_ids |= new_ids
    return out
