"""End-to-end tests on a small hand-built dataset whose expected output is
worked out by hand below. Covers: merge + batch ownership, rejection,
effective disposition (override beats set, unknown codes ignored), every
bucket, IST date boundaries, late-arriving leads (orphan -> linked), event
dedupe across drops, idempotent re-ingest, incremental == all-at-once, PII
at rest, append-only ledger."""
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from solution.cli import main

MOTOR_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def ev(eid, etype, call, at, phone=None, **payload):
    e = {"event_id": eid, "event_type": etype, "workflow_id": "motor_renewal", "call_id": call,
         "occurred_at": at}
    if phone is not None:
        e["phone"] = phone
    e["payload"] = payload
    return json.dumps(e)


DROP1_LEADS = {
    "batch_001.csv": "\n".join([
        MOTOR_HEADER,
        "Asha Rao,9000000001,MOT/1,01/12/2026,1000,hi",
        "Ravi Iyer,+91 90000 00002,MOT/2,,,",
        "Meera Nair,09000000003,MOT/3,,N/A,ta",          # 1 field warning
        "Kiran Das,9000000004,MOT/4,,,en",
        ",,,,,",                                          # blank: skipped, not counted
        "Asha R.,91-90000-00001,MOT/1,,1200,",            # dup of MOT/1 (row 5)
        "Bad Phone,12345,MOT/9,,,",                       # rejected row 6
    ]) + "\n",
}
DROP2_LEADS = {
    # renamed + reordered columns, extra column
    "batch_002.csv": "\n".join([
        "Policy No,Mobile No,Insured Name,notes",
        "MOT/5,9000000005,Late Lead,x",
        "MOT/2,9000000002,Ravi Iyer,dup",                 # dup of MOT/2 -> stays batch_001
    ]) + "\n",
}
DROP1_EVENTS = [
    # L1: callback set, later override to renewed -> effective renewed (terminal, success)
    ev("e01", "call.initiated", "c1", "2026-10-01 10:00:00", "9000000001"),
    ev("e02", "call.connected", "c1", "2026-10-01T04:30:10Z", "+919000000001"),
    ev("e03", "disposition.set", "c1", "2026-10-01T10:05:00+05:30", "9000000001",
       disposition_code="callback_requested"),
    ev("e04", "disposition.override", "c1", "2026-10-02T09:00:00+05:30",
       disposition_code="renewed", overridden_by="sup_1", reason="qa_review"),
    ev("e05", "disposition.set", "c1", "2026-10-03T09:00:00+05:30", "9000000001",
       disposition_code="not_interested"),  # later set, but an override always wins
    # L2: only a telephony failure; 19:00Z = 00:30 IST next day
    ev("e06", "call.initiated", "c2", "2026-10-01T19:00:00Z", "9000000002"),
    ev("e07", "call.telephony_failed", "c2", "2026-10-01T19:00:05Z", "9000000002", reason="sip_503"),
    # L3: dialed + callback, then a failed call; plus an unknown code
    ev("e08", "call.initiated", "c3", "2026-10-01 11:00:00", "9000000003"),
    ev("e09", "call.connected", "c3", "2026-10-01 11:00:10", "9000000003"),
    ev("e10", "disposition.set", "c3", "2026-10-01 11:05:00", "9000000003",
       disposition_code="callback_requested"),
    ev("e11", "disposition.set", "c3", "2026-10-01 11:06:00", "9000000003",
       disposition_code="RENEWD"),  # unknown -> counted and ignored
    ev("e12", "call.initiated", "c4", "2026-10-03 12:00:00", "9000000003"),
    ev("e13", "call.telephony_failed", "c4", "2026-10-03 12:00:02", "9000000003"),
    # L5: events arrive a drop before the lead
    ev("e14", "call.initiated", "c5", "2026-10-01 15:00:00", "9000000005"),
    ev("e15", "call.connected", "c5", "2026-10-01 15:00:05", "9000000005"),
    ev("e16", "disposition.set", "c5", "2026-10-01 15:09:00", "9000000005", disposition_code="renewed"),
    # true orphans
    ev("e17", "call.initiated", "c6", "2026-10-01 16:00:00", "9111111111"),
    ev("e18", "disposition.override", "c_unknown", "2026-10-01 16:00:00", disposition_code="renewed"),
    '{"event_id": "e19", "event_ty',                      # malformed
    ev("e01", "call.initiated", "c1", "2026-10-01 10:00:00", "9000000001"),  # duplicate
]
DROP2_EVENTS = [
    ev("e06", "call.initiated", "c2", "2026-10-01T19:00:00Z", "9000000002"),  # dup across drops
    ev("e20", "call.ended", "c3", "2026-10-01 11:04:00", "9000000003", duration_sec=230, end_reason="completed"),
    "",                                                   # empty line: not counted
]


def write_drop(root: Path, name: str, leads: dict, events: list[str]) -> Path:
    d = root / name
    (d / "leads" / "motor_renewal").mkdir(parents=True)
    for fn, text in leads.items():
        (d / "leads" / "motor_renewal" / fn).write_text(text, encoding="utf-8")
    (d / "events").mkdir()
    (d / "events" / f"events_{name}.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    return d


@pytest.fixture
def drops(tmp_path):
    data = tmp_path / "data"
    return (write_drop(data, "drop_1", DROP1_LEADS, DROP1_EVENTS),
            write_drop(data, "drop_2", DROP2_LEADS, DROP2_EVENTS))


def run(db: Path, *args) -> int:
    return main(["--db", str(db), *map(str, args)])


def read_csv(p: Path):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def reports(db: Path, out: Path) -> dict:
    assert run(db, "report", "--out", out) == 0
    return {p.name: p.read_text() for p in sorted(out.iterdir())}


def test_full_expected_output(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    assert run(db, "ingest", drops[0]) == 0
    assert run(db, "ingest", drops[1]) == 0
    out = tmp_path / "out"
    reports(db, out)

    leads = {r["external_ref"]: r for r in read_csv(out / "lead_report.csv")}
    assert list(leads) == ["MOT/1", "MOT/2", "MOT/3", "MOT/4", "MOT/5"]
    assert leads["MOT/1"] == {
        "workflow_id": "motor_renewal", "external_ref": "MOT/1", "batch_id": "batch_001",
        "phone_masked": "+91******0001", "bucket": "completed", "total_calls": "1",
        "connected_calls": "1", "last_call_at": "2026-10-01T10:00:00+05:30",
        "current_disposition": "renewed", "ever_success": "true"}
    assert (leads["MOT/2"]["bucket"], leads["MOT/2"]["batch_id"]) == ("in_progress_telephony_failed", "batch_001")
    assert leads["MOT/2"]["last_call_at"] == "2026-10-02T00:30:00+05:30"
    assert leads["MOT/3"]["bucket"] == "in_progress_dialed"
    assert leads["MOT/3"]["total_calls"] == "2" and leads["MOT/3"]["connected_calls"] == "1"
    assert leads["MOT/3"]["current_disposition"] == "callback_requested"  # c4 has none; falls back to c3
    assert leads["MOT/3"]["last_call_at"] == "2026-10-03T12:00:00+05:30"
    assert (leads["MOT/4"]["bucket"], leads["MOT/4"]["last_call_at"], leads["MOT/4"]["ever_success"]) \
        == ("yet_to_reach_out", "", "false")
    assert (leads["MOT/5"]["bucket"], leads["MOT/5"]["batch_id"]) == ("completed", "batch_002")

    assert (out / "batch_summary.csv").read_text() == (
        "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,"
        "in_progress_dialed,completed\n"
        "motor_renewal,batch_001,4,1,1,1,1\n"
        "motor_renewal,batch_002,1,0,0,0,1\n")

    assert (out / "daily_overview.csv").read_text() == (
        "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads\n"
        "motor_renewal,2026-10-01,3,3,1.0000,2\n"
        "motor_renewal,2026-10-02,1,0,0.0000,0\n"
        "motor_renewal,2026-10-03,1,0,0.0000,0\n")

    assert (out / "rejections.csv").read_text() == (
        "workflow_id,batch_id,row_number,reason\nmotor_renewal,batch_001,6,invalid_phone\n")

    assert json.loads((out / "data_quality.json").read_text()) == {
        "lead_rows_total": 8,
        "lead_rows_rejected": 1,
        "rejections_by_reason": {"invalid_phone": 1},
        "duplicate_lead_rows_merged": 2,
        "field_warnings": 1,
        "events_total": 22,        # 20 + 2 non-empty lines
        "malformed_events": 1,
        "duplicate_events_dropped": 2,
        "orphan_events": 2,        # e17 (unknown phone), e18 (override for unknown call)
        "unknown_disposition_events": 1,
    }


def test_orphans_become_linked_when_lead_arrives(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    q1 = json.loads(reports(db, tmp_path / "o1")["data_quality.json"])
    assert q1["orphan_events"] == 5  # L5's 3 events + 2 true orphans
    run(db, "ingest", drops[1])
    q2 = json.loads(reports(db, tmp_path / "o2")["data_quality.json"])
    assert q2["orphan_events"] == 2


def test_reingest_changes_nothing_and_incremental_equals_all_at_once(tmp_path, drops):
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    run(a, "ingest", drops[0])
    run(a, "ingest", drops[0])
    run(a, "ingest", drops[1])
    run(a, "ingest", drops[0], drops[1])
    run(b, "ingest", drops[0].parent)  # parent dir: every drop_* in order
    assert reports(a, tmp_path / "ra") == reports(b, tmp_path / "rb")
    with sqlite3.connect(a) as c:
        assert c.execute("SELECT COUNT(*) FROM source_file").fetchone()[0] == 4


def test_merge_last_non_null_wins_and_defaults_after_merge(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    with sqlite3.connect(db) as c:
        attrs = json.loads(c.execute(
            "SELECT attrs_json FROM lead WHERE external_ref='MOT/1'").fetchone()[0])
        ravi = json.loads(c.execute(
            "SELECT attrs_json FROM lead WHERE external_ref='MOT/2'").fetchone()[0])
    # dup row had premium 1200 and empty language -> premium overwritten, 'hi' kept
    assert attrs == {"policy_number": "MOT/1", "policy_expiry": "2026-12-01",
                     "premium_amount": "1200.00", "preferred_language": "hi"}
    assert ravi["preferred_language"] == "en"  # schema default


def test_no_plaintext_pii_in_database(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    run(db, "ingest", drops[1])
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = db.read_bytes()
    for secret in [b"9000000001", b"90000 00001", b"Asha", b"Ravi Iyer", b"Late Lead", b"9111111111"]:
        assert secret not in raw, secret


def test_wrong_key_is_refused(tmp_path, drops, monkeypatch):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    monkeypatch.setenv("AU_PII_KEY", "A" * 43 + "=")  # some other 32-byte key
    assert run(db, "report", "--out", tmp_path / "o") == 2


def test_ledger_is_append_only(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    with sqlite3.connect(db) as c:
        for stmt in ("UPDATE ledger_event SET event_type='x'", "DELETE FROM ledger_event",
                     "UPDATE ledger_event_detail SET payload_json='{}'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                c.execute(stmt)


def test_batch_with_same_name_but_different_content_is_refused(tmp_path, drops):
    db = tmp_path / "l.sqlite"
    run(db, "ingest", drops[0])
    clash = write_drop(tmp_path / "other", "drop_9",
                       {"batch_001.csv": MOTOR_HEADER + "\nX,9000000077,MOT/77,,,\n"}, [])
    assert run(db, "ingest", clash) == 1
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM lead WHERE external_ref='MOT/77'").fetchone()[0] == 0
