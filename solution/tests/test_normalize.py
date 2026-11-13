from datetime import datetime, timezone

import pytest

from solution.normalize import (mask_phone, normalize_date, normalize_decimal, normalize_enum,
                                normalize_header, normalize_phone, parse_timestamp, to_ist_iso)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
    "(+91) 9876543210", "+919876543210", "919876543210", " 98765-43210 ",
])
def test_phone_all_generator_formats(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad leading digit
    "022-2345-6789",      # landline: 11 digits, drop 0 -> starts with 2
    "N/A", "", "+1 415 555 0134",
    "0919876543210",      # 13 digits
    "91987654321",        # 11 digits starting 9, not 0
    "٩٨٧٦٥٤٣٢١٠",          # non-ASCII digits are not digits for us
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_12_digits_starting_91_only_strips_once():
    # 91 + 91XXXXXXXX: stripping once leaves 9198..., which starts with 9 -> valid
    assert normalize_phone("919198765432") == "+919198765432"


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),     # day first
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),
    ("29/02/2028", "2028-02-29"),     # leap year
])
def test_date_valid(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2026", "2026-13-01", "soon", "TBD", "12/31/2026",
    "01-Dex-2026", "2026/12/01", "",
])
def test_date_invalid(raw):
    assert normalize_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12000", "12000.00"), ("₹12,345", "12345.00"), ("Rs. 500", "500.00"), ("Rs500", "500.00"),
    ("rs.  7", "7.00"), ("INR 99.5", "99.50"), ("inr1,00,000", "100000.00"),
    ("10.005", "10.01"),  # half-up, not banker's rounding
    ("0", "0.00"),
])
def test_decimal_valid(raw, expected):
    assert normalize_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "1e5", "₹", "1.2.3"])
def test_decimal_invalid(raw):
    assert normalize_decimal(raw) is None


def test_enum():
    vals = ["en", "hi"]
    assert normalize_enum("HI", vals) == "hi"
    assert normalize_enum(" en ", vals) == "en"
    assert normalize_enum("Hindi", vals) is None


@pytest.mark.parametrize("raw,expected", [
    ("Mobile No", "mobile no"), ("  mobile__no ", "mobile no"), ("Mobile _ \t No", "mobile no"),
    ("policy_number", "policy number"),
])
def test_header(raw, expected):
    assert normalize_header(raw) == expected


def test_timestamps_three_forms_agree():
    a = parse_timestamp("2026-10-05T07:50:21Z")
    b = parse_timestamp("2026-10-05T13:20:21+05:30")
    c = parse_timestamp("2026-10-05 13:20:21")  # naive means IST
    assert a == b == c == datetime(2026, 10, 5, 7, 50, 21, tzinfo=timezone.utc)
    assert to_ist_iso(a) == "2026-10-05T13:20:21+05:30"


def test_ist_date_crosses_midnight():
    # 20:00 UTC is 01:30 next day in IST
    assert to_ist_iso(parse_timestamp("2026-10-05T20:00:00Z")).startswith("2026-10-06")


@pytest.mark.parametrize("raw", [None, "", "yesterday", 12345])
def test_timestamp_invalid(raw):
    assert parse_timestamp(raw) is None
