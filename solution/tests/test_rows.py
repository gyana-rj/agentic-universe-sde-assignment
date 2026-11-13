"""Row-level lead validation and event-line parsing (pure functions)."""
from solution.ingest_events import parse_line
from solution.ingest_leads import iter_data_rows, validate_row
from solution.schemas import DEFAULT_SCHEMA_DIR, load_schemas

SCHEMAS = load_schemas(DEFAULT_SCHEMA_DIR)
MOTOR = SCHEMAS["motor_renewal"]


def row(**kw):
    base = {"customer_name": "Asha Rao", "phone": "9876543210", "policy_number": "MOT/1",
            "policy_expiry": "01/12/2026", "premium_amount": "₹1,000", "preferred_language": "HI"}
    base.update(kw)
    return base


def test_valid_row_normalised():
    r = validate_row(MOTOR, row())
    assert r.reason is None and r.warnings == []
    assert r.values == {"customer_name": "Asha Rao", "phone": "+919876543210",
                        "policy_number": "MOT/1", "policy_expiry": "2026-12-01",
                        "premium_amount": "1000.00", "preferred_language": "hi"}


def test_first_failure_in_schema_order_wins():
    # name missing AND phone invalid AND ref missing -> name comes first
    assert validate_row(MOTOR, row(customer_name="  ", phone="N/A", policy_number="")).reason \
        == "missing_required:customer_name"
    assert validate_row(MOTOR, row(phone="N/A", policy_number="")).reason == "invalid_phone"
    assert validate_row(MOTOR, row(phone="")).reason == "missing_required:phone"
    assert validate_row(MOTOR, row(policy_number="")).reason == "missing_required:policy_number"


def test_optional_invalid_is_null_plus_warning():
    r = validate_row(MOTOR, row(policy_expiry="31/02/2026", premium_amount="N/A", preferred_language="Hindi"))
    assert r.reason is None
    assert r.warnings == ["policy_expiry", "premium_amount", "preferred_language"]
    assert r.values["policy_expiry"] is None and r.values["preferred_language"] is None


def test_optional_missing_is_not_a_warning_and_no_default_yet():
    r = validate_row(MOTOR, row(preferred_language=""))
    assert r.warnings == [] and r.values["preferred_language"] is None  # default applied after merge


def test_rejected_row_carries_no_warnings():
    r = validate_row(MOTOR, row(premium_amount="N/A", policy_number=""))
    assert r.values is None and r.warnings == []


def test_header_aliases_extra_columns_and_blank_rows():
    text = ("Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes\r\n"
            "en,Asha,MOT/1,9876543210,01-Dec-2026,Rs. 5,VIP\r\n"
            ",,,,,,\r\n"
            "  ,   ,,,,,\r\n"
            "hi,Ravi,MOT/2,09876500000,,,\r\n")
    rows = list(iter_data_rows(text, MOTOR))
    assert [n for n, _ in rows] == [1, 2]  # blank rows skipped and not counted
    assert rows[0][1] == {"preferred_language": "en", "customer_name": "Asha", "policy_number": "MOT/1",
                          "phone": "9876543210", "policy_expiry": "01-Dec-2026", "premium_amount": "Rs. 5"}
    assert "agent_notes" not in rows[0][1]


def test_row_with_only_unmapped_cell_is_not_blank():
    text = "customer_name,phone,policy_number,agent_notes\n,,,VIP\n"
    rows = list(iter_data_rows(text, MOTOR))
    assert len(rows) == 1
    assert validate_row(MOTOR, rows[0][1]).reason == "missing_required:customer_name"


def test_short_rows_are_padded():
    rows = list(iter_data_rows("customer_name,phone,policy_number\nAsha,9876543210\n", MOTOR))
    assert validate_row(MOTOR, rows[0][1]).reason == "missing_required:policy_number"


# ---------------------------------------------------------------- events

def test_parse_line_malformed():
    assert parse_line('{"event_id": "evt_1", "event_ty') is None          # truncated
    assert parse_line('[1, 2]') is None                                   # not an object
    assert parse_line('{"event_type": "call.initiated"}') is None         # no event_id
    assert parse_line('{"event_id": "", "occurred_at": "2026-10-01T00:00:00Z"}') is None
    assert parse_line('{"event_id": "e1", "occurred_at": "soon"}') is None


def test_parse_line_strips_phone_from_stored_body():
    ev = parse_line('{"event_id": "e1", "event_type": "disposition.set", "workflow_id": "w", '
                    '"call_id": "c1", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210", '
                    '"payload": {"disposition_code": "renewed"}, "surprise": 1}')
    assert ev.event_id == "e1" and ev.disposition_code == "renewed"
    assert ev.phone_raw == "9876543210"
    assert ev.extra == {"surprise": 1}
    assert ev.occurred_at.isoformat() == "2026-10-01T04:30:00+00:00"
