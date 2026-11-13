"""Pure value-normalisation rules from the spec. No I/O, no database.

Every function returns the canonical value, or ``None`` when the input is
present but invalid. Callers decide what "invalid" means for a row (reject,
or null + warning). Missing values (empty after trimming) are handled by the
caller before these are called.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30), "IST")

# ---------------------------------------------------------------- headers

_HEADER_SEP = re.compile(r"[_\s]+")


def normalize_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' / whitespace to one space."""
    return _HEADER_SEP.sub(" ", h.strip().lower()).strip()


# ---------------------------------------------------------------- phone

_NON_DIGIT = re.compile(r"[^0-9]")  # ASCII only: '\D' would keep non-ASCII digits


def normalize_phone(raw: object) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def mask_phone(canonical: str) -> str:
    return "+91******" + canonical[-4:]


# ---------------------------------------------------------------- date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DATE_PATTERNS = [
    # (regex, (year_group, month_group, day_group)); month group may be a name
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), (1, 2, 3)),          # YYYY-MM-DD
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), (3, 2, 1)),          # DD/MM/YYYY
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), (3, 2, 1)),          # DD-MM-YYYY
    (re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$"), (3, 2, 1)),      # DD-Mon-YYYY
]


def normalize_date(raw: str) -> str | None:
    """Day-first date -> 'YYYY-MM-DD', or None if unparseable / impossible."""
    s = raw.strip()
    for rx, (yg, mg, dg) in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        month_s = m.group(mg)
        month = _MONTHS.get(month_s.lower()) if month_s.isalpha() else int(month_s)
        if month is None:
            return None
        try:
            return date(int(m.group(yg)), month, int(m.group(dg))).isoformat()
        except ValueError:  # 31/02, month 13, ...
            return None
    return None


# ---------------------------------------------------------------- decimal

_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")


def normalize_decimal(raw: str) -> str | None:
    """'₹12,000' / 'Rs. 500' / 'INR 99.5' -> '12000.00'. Must be >= 0."""
    s = _CURRENCY_PREFIX.sub("", raw.strip(), count=1).replace(",", "").strip()
    if not _PLAIN_NUMBER.match(s):  # rejects '', '-', '-5', 'N/A', '1e5'
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- enum

def normalize_enum(raw: str, allowed: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {a.lower() for a in allowed} else None


# ---------------------------------------------------------------- timestamps

def parse_timestamp(raw: object) -> datetime | None:
    """'...Z', '...+05:30' or naive (= IST). Returns an aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_ist_iso(dt_utc: datetime) -> str:
    """ISO-8601 in IST with second precision: 2026-10-14T17:55:24+05:30."""
    return dt_utc.astimezone(IST).replace(microsecond=0).isoformat()
