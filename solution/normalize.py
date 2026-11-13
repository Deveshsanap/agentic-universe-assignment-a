"""Shared field normalisation for Assignment A.

Everything in here is pure: a value in, a clean value (or None) out.
No database, no files, so it is easy to test.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Month names for the DD-Mon-YYYY date format, e.g. "01-Dec-2026".
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def normalize_header(header: str) -> str:
    """Lower-case, trim, collapse every run of _ or whitespace to one space."""
    return re.sub(r"[_\s]+", " ", header.strip().lower())


def normalize_phone(raw: str) -> str | None:
    """Return the canonical +91XXXXXXXXXX form, or None if invalid.

    Strip every non-digit. Drop a leading 91 (12 digits) or a leading 0
    (11 digits). The result is valid only if it is exactly 10 digits
    starting with 6, 7, 8 or 9.
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def phone_hash(canonical_phone: str) -> str:
    """SHA-256 of the canonical phone. Used to match events to leads
    without ever storing the phone number itself."""
    return hashlib.sha256(canonical_phone.encode("utf-8")).hexdigest()


def phone_masked(canonical_phone: str) -> str:
    """+91****** followed by the last 4 digits, for lead_report.csv."""
    return "+91******" + canonical_phone[-4:]


def parse_date(raw: str) -> str | None:
    """Parse one of the four accepted formats; return YYYY-MM-DD or None.

    Accepted: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    Dates are day-first; impossible dates (31/02) are invalid.
    """
    text = raw.strip()
    # DD-Mon-YYYY, month name case-insensitive, e.g. 01-dec-2026.
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", text)
    if m:
        day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        if mon is None:
            return None
        try:
            return datetime(year, mon, day).date().isoformat()
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_decimal(raw: str) -> str | None:
    """Parse a money value; return a string with exactly 2 decimals or None.

    Optional prefix: ₹, Rs, Rs. or INR (case-insensitive, optional space).
    Commas are thousands separators. Must be >= 0. Rounded to 2 places.
    """
    text = raw.strip()
    text = re.sub(r"^(₹|Rs\.?|INR)\s*", "", text, flags=re.IGNORECASE)
    text = text.replace(",", "")
    if not text or not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    try:
        value = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return format(value, ".2f")


def parse_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive enum match; store lower-case, or None if no match."""
    lowered = raw.strip().lower()
    return lowered if lowered in [v.lower() for v in values] else None


def parse_timestamp(raw: str) -> datetime | None:
    """Parse an event timestamp into an aware datetime, or None.

    Three forms: UTC (...Z), with an offset (...+05:30), and naive
    (2026-10-03 10:01:00) — naive means IST.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def ist_date(dt: datetime) -> str:
    """IST calendar date (YYYY-MM-DD) of an aware datetime."""
    return dt.astimezone(IST).date().isoformat()


def ist_iso(dt: datetime) -> str:
    """ISO-8601 timestamp in IST, e.g. 2026-10-14T17:55:24+05:30."""
    return dt.astimezone(IST).isoformat(timespec="seconds")
