"""Cleaning and validation for raw invoice rows.

Every rule here is a small pure function so it can be unit tested without a
database or a file. `clean_frame` wires them together and splits the input into
rows that are safe to load and rows that go to quarantine with a reason.

Money is parsed to Decimal and stays Decimal. Never float: a tenth of a cent of
drift is a real defect in an AP system.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd

__all__ = [
    "split_invoices",
    "VALID_STATUSES",
    "normalize_vendor_name",
    "parse_amount",
    "parse_date",
    "normalize_invoice_number",
    "resolve_due_date",
    "clean_frame",
]

# Legal-entity suffixes stripped before comparing vendor names. Order matters:
# longer forms first, so "incorporated" is not left as "orated" by "inc".
_LEGAL_SUFFIXES = [
    "incorporated", "corporation", "limited", "company", "holdings",
    "international", "worldwide", "services", "solutions", "group",
    "llc", "lllp", "llp", "lp", "plc", "pte", "pty", "inc", "corp",
    "ltd", "gmbh", "sarl", "srl", "bv", "nv", "ag", "sa", "co",
]
_SUFFIX_RE = re.compile(
    r"\b(" + "|".join(_LEGAL_SUFFIXES) + r")\b", flags=re.IGNORECASE
)

# Date formats tried in order. Ambiguous DD/MM vs MM/DD is resolved as US-style
# month-first, which matches the source systems this pipeline targets.
_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%d-%b-%Y", "%d %b %Y",
    "%b %d, %Y", "%B %d, %Y", "%m/%d/%y", "%Y%m%d", "%d.%m.%Y",
]

_CURRENCY_CHARS = re.compile(r"[$£€¥,\s]")
_TRAILING_SIGN = re.compile(r"^(?P<body>[\d.]+)\s*(?P<sign>CR|DR|-|\+)$", re.IGNORECASE)


def normalize_vendor_name(raw: object) -> str:
    """Collapse a vendor name to a comparable key.

    "ACME Corp.", "Acme Corporation" and "  acme   corp  " all become "acme".
    Returns "" for anything empty, which the caller treats as a rejection.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = str(raw)
    # Fold accents so "Nestlé" and "Nestle" match.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " and ")
    # Drop periods without leaving a gap, so "S.A." becomes "sa" and is then
    # recognised as a legal suffix. Doing this after the generic punctuation
    # pass would leave "s a", which the suffix pattern cannot match.
    text = text.replace(".", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = _SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_amount(raw: object) -> Decimal | None:
    """Parse a money string to Decimal, or None if it is not a number.

    Handles "$1,234.56", "(450.00)" and "450.00-" as negatives, and bare floats
    that pandas already coerced.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, Decimal):
        return raw.quantize(Decimal("0.01"))
    if isinstance(raw, (int, float)):
        return Decimal(str(raw)).quantize(Decimal("0.01"))

    text = str(raw).strip()
    if not text:
        return None

    negative = False
    # Accounting notation: parentheses mean a credit.
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _CURRENCY_CHARS.sub("", text)
    if not text:
        return None

    # Trailing sign or CR/DR marker, e.g. "450.00-" or "450.00CR".
    match = _TRAILING_SIGN.match(text)
    if match:
        text = match.group("body")
        if match.group("sign").upper() in {"CR", "-"}:
            negative = True

    if text.startswith("-"):
        negative = True
        text = text[1:]

    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return -value if negative else value


def parse_date(raw: object) -> date | None:
    """Parse a date using an explicit format list. None if unparseable.

    Deliberately does not fall back to a fuzzy parser: silently guessing at
    "03/04/2024" across formats is how ledgers end up off by a month.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = str(raw).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_invoice_number(raw: object) -> str:
    """Mirror of the SQL function: strip formatting and leading zeros."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = re.sub(r"[^a-z0-9]", "", str(raw).lower())
    # Strip leading zeros from each digit run, so INV-0042, INV42 and 0042 all
    # collapse together. A plain lstrip("0") would miss the zeros in "inv0042".
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", text)


def resolve_due_date(
    invoice_date: date | None,
    due_date: date | None,
    terms_days: int | None,
) -> date | None:
    """Use the stated due date when present, otherwise derive it from terms."""
    if due_date is not None:
        return due_date
    if invoice_date is None:
        return None
    return invoice_date + timedelta(days=terms_days if terms_days is not None else 30)


# Mirrors the CHECK constraint in sql/001_schema.sql. An unmapped value is
# rejected rather than defaulted: quietly turning an unrecognised status into
# "open" would inflate the payables balance with invoices that may already be
# settled.
VALID_STATUSES = {"open", "paid", "void", "on_hold"}
_STATUS_SYNONYMS = {
    "": "open", "outstanding": "open", "unpaid": "open", "posted": "open",
    "settled": "paid", "closed": "paid", "complete": "paid",
    "cancelled": "void", "canceled": "void", "voided": "void",
    "hold": "on_hold", "onhold": "on_hold", "on hold": "on_hold",
    "disputed": "on_hold",
}


def _parse_status(raw: object) -> str | None:
    """Map a status string onto the four values the schema allows."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "open"
    text = str(raw).strip().lower().replace("-", "_")
    if text in VALID_STATUSES:
        return text
    return _STATUS_SYNONYMS.get(text.replace("_", " "), _STATUS_SYNONYMS.get(text))


def _to_int(raw: object, default: int) -> int:
    """Best-effort int coercion that never raises."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _validate(row: dict) -> str | None:
    """Return a rejection reason, or None if the row is loadable."""
    if not row["vendor_normalized"]:
        return "missing_vendor"
    if not row["invoice_number"]:
        return "missing_invoice_number"
    if row["invoice_date"] is None:
        return "unparseable_invoice_date"
    if row["amount"] is None:
        return "unparseable_amount"
    if row["amount"] == 0:
        return "zero_amount"
    if row["status"] is None:
        return "unrecognized_status"
    if row["due_date"] is None:
        return "no_due_date_and_no_terms"
    if row["due_date"] < row["invoice_date"]:
        return "due_date_before_invoice_date"
    return None


def clean_frame(
    df: pd.DataFrame,
    source_file: str,
    default_terms_days: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clean a raw invoice frame.

    Returns (clean, rejected). Rejected rows keep their original values plus a
    `reject_reason` column so an AP clerk can fix and resubmit them.
    """
    records, rejects = [], []

    for position, (_, raw) in enumerate(df.iterrows(), start=2):  # row 1 is the header
        terms = raw.get("payment_terms_days")
        try:
            terms_days = int(terms) if terms not in (None, "") and pd.notna(terms) else None
        except (TypeError, ValueError):
            terms_days = None
        if terms_days is None:
            terms_days = default_terms_days

        invoice_date = parse_date(raw.get("invoice_date"))
        cleaned = {
            "source_file": source_file,
            "source_row": position,
            "vendor_name": str(raw.get("vendor_name", "")).strip(),
            "vendor_normalized": normalize_vendor_name(raw.get("vendor_name")),
            "invoice_number": str(raw.get("invoice_number", "")).strip(),
            "invoice_number_normalized": normalize_invoice_number(raw.get("invoice_number")),
            "invoice_date": invoice_date,
            "due_date": resolve_due_date(
                invoice_date, parse_date(raw.get("due_date")), terms_days
            ),
            "payment_terms_days": terms_days,
            "amount": parse_amount(raw.get("amount")),
            "currency": (str(raw.get("currency", "USD")).strip().upper() or "USD")[:3],
            "status": _parse_status(raw.get("status")),
            "gl_code": (str(raw.get("gl_code", "")).strip() or None),
            "tax_id": (str(raw.get("tax_id", "")).strip() or None),
            # Line-item grain. The source CSV repeats the header fields above on
            # every line, so these are the only per-row values.
            "line_number": _to_int(raw.get("line_number"), default=1),
            "line_description": (str(raw.get("line_description", "")).strip() or None),
            "quantity": parse_amount(raw.get("quantity")) or Decimal("1"),
            "unit_price": parse_amount(raw.get("unit_price")),
            "line_total": parse_amount(raw.get("line_total")),
        }

        reason = _validate(cleaned)
        if reason:
            original = raw.to_dict()
            original["reject_reason"] = reason
            original["source_row"] = position
            rejects.append(original)
        else:
            records.append(cleaned)

    clean_df = pd.DataFrame(records)
    reject_df = pd.DataFrame(rejects)

    # The source CSV is line-grain: header fields repeat on every line of a
    # multi-line invoice. So the in-file uniqueness key must include the line
    # number, or a three-line invoice would lose two of its lines here.
    # Genuine same-vendor-same-number duplicates are caught by DUP_EXACT in SQL
    # after load, where both copies are visible and can be reported as a pair.
    #
    # Note the key uses the RAW invoice number, not the normalized one. A row
    # repeated byte-for-byte is a data-entry error and belongs in quarantine.
    # "INV-00577" arriving alongside "INV577" is a business finding: both must
    # load so DUP_EXACT can report them as a pair on the dashboard. Normalizing
    # here would silently swallow exactly the duplicates this project exists to
    # surface.
    if not clean_df.empty:
        key = ["vendor_normalized", "invoice_number", "line_number"]
        dupe_mask = clean_df.duplicated(subset=key, keep="first")
        if dupe_mask.any():
            in_file_dupes = clean_df[dupe_mask].copy()
            in_file_dupes["reject_reason"] = "repeated_line_number_within_invoice"
            reject_df = pd.concat([reject_df, in_file_dupes], ignore_index=True)
            clean_df = clean_df[~dupe_mask].reset_index(drop=True)

    return clean_df, reject_df


def split_invoices(clean_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split line-grain cleaned rows into invoice headers and invoice lines.

    Header fields are taken from the first row of each group; they are repeated
    identically across the group in the source format.
    """
    if clean_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Raw invoice number, matching the dedupe key in clean_frame. Grouping on
    # the normalized form here would merge a duplicate pair back into a single
    # invoice before the rule engine ever sees it.
    key = ["vendor_normalized", "invoice_number"]
    header_cols = [
        "vendor_name", "vendor_normalized", "tax_id", "payment_terms_days",
        "invoice_number", "invoice_number_normalized", "invoice_date",
        "due_date", "amount", "currency", "status", "gl_code",
        "source_file", "source_row",
    ]
    headers = (
        clean_df.groupby(key, as_index=False, sort=False)[header_cols]
        .first()
    )
    lines = clean_df[
        key + ["line_number", "line_description", "quantity", "unit_price",
               "line_total", "gl_code"]
    ].copy()
    return headers, lines
