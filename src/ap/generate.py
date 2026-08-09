"""Generate synthetic, deliberately dirty invoice CSVs.

Standard library only, and seeded, so `make data` produces the same file on any
machine without a pip install. The dirt is intentional: inconsistent vendor
spellings, mixed date formats, accounting-notation negatives, missing due dates
and a seeded population of duplicates for the rule engine to find.

NOTHING HERE IS REAL. Never point this pipeline at live vendor data and commit
the output.
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

# Each tuple is (canonical name, [spelling variants seen in the wild]).
VENDOR_POOL = [
    ("Acme Corporation", ["Acme Corporation", "ACME Corp.", "acme corp", "ACME CORPORATION"]),
    ("Globex LLC", ["Globex LLC", "Globex L.L.C.", "GLOBEX, LLC", "globex llc"]),
    ("Initech Inc", ["Initech Inc", "Initech, Inc.", "INITECH INC", "Initech"]),
    ("Umbrella Health Ltd", ["Umbrella Health Ltd", "Umbrella Health Limited", "UMBRELLA HEALTH"]),
    ("Stark Industries", ["Stark Industries", "STARK INDUSTRIES", "Stark Industries Inc."]),
    ("Wayne Enterprises", ["Wayne Enterprises", "Wayne Enterprises Inc", "WAYNE ENTERPRISES"]),
    ("Soylent Foods Co", ["Soylent Foods Co", "Soylent Foods Company", "SOYLENT FOODS CO."]),
    ("Nestle SA", ["Nestle SA", "Nestlé S.A.", "NESTLE SA"]),
    ("Cyberdyne Systems", ["Cyberdyne Systems", "Cyberdyne Sys.", "CYBERDYNE SYSTEMS"]),
    ("Tyrell Corp", ["Tyrell Corp", "Tyrell Corporation", "TYRELL CORP."]),
    ("Vandelay Industries", ["Vandelay Industries", "Vandelay Ind.", "VANDELAY INDUSTRIES"]),
    ("Hooli", ["Hooli", "Hooli Inc", "HOOLI INC."]),
    ("Pied Piper LLC", ["Pied Piper LLC", "Pied Piper, LLC", "PIED PIPER"]),
    ("Massive Dynamic", ["Massive Dynamic", "Massive Dynamic Corp", "MASSIVE DYNAMIC"]),
    ("Aperture Science", ["Aperture Science", "Aperture Science Inc", "APERTURE SCIENCE"]),
]

GL_CODES = ["5010", "5020", "5100", "6000", "6100", "6200", "7000", "7500"]

LINE_DESCRIPTIONS = [
    "Professional services", "Software licence renewal", "Hardware maintenance",
    "Consulting hours", "Freight and handling", "Office supplies",
    "Cloud hosting", "Training delivery", "Equipment rental", "Contract labour",
]

DATE_FORMATTERS = [
    lambda d: d.strftime("%Y-%m-%d"),
    lambda d: d.strftime("%m/%d/%Y"),
    lambda d: d.strftime("%d-%b-%Y"),
    lambda d: d.strftime("%b %d, %Y"),
    lambda d: d.strftime("%Y/%m/%d"),
]

FIELDNAMES = [
    "invoice_number", "vendor_name", "tax_id", "invoice_date", "due_date",
    "payment_terms_days", "amount", "currency", "status", "gl_code",
    "line_number", "line_description", "quantity", "unit_price", "line_total",
]


def _format_amount(value: Decimal, rng: random.Random) -> str:
    """Render an amount in one of several messy real-world styles."""
    if value < 0:
        style = rng.choice(["paren", "trailing", "leading"])
        magnitude = -value
        if style == "paren":
            return f"({magnitude:,.2f})"
        if style == "trailing":
            return f"{magnitude:.2f}-"
        return f"-{magnitude:.2f}"
    style = rng.choice(["plain", "comma", "dollar", "dollar_comma"])
    if style == "plain":
        return f"{value:.2f}"
    if style == "comma":
        return f"{value:,.2f}"
    if style == "dollar":
        return f"${value:.2f}"
    return f"${value:,.2f}"


def _line_amount(rng: random.Random) -> Decimal:
    """Draw a line total from a log-normal distribution.

    Real invoice values are heavily right-skewed: lots of small ones, a long
    thin tail of large ones. A uniform draw put two thirds of invoices over the
    10,000 approval threshold, which made AMT_THRESHOLD flag most of the ledger
    and stop being a useful control.

    These parameters give a median near 800 and roughly 2% of lines above
    10,000.
    """
    value = rng.lognormvariate(6.7, 1.3)
    return Decimal(str(round(min(value, 250_000), 2)))


def _payment_status(rng: random.Random, due: date, today: date) -> str:
    """Decide whether an invoice has been paid, based on how old it is.

    Without this every invoice ever generated stayed open and the aging report
    was 40% in the 90+ bucket. A real ledger is mostly current, because most
    invoices get paid; the buckets are a tail, not the bulk.
    """
    days_past_due = (today - due).days
    if days_past_due < 0:
        paid_probability = 0.10
    elif days_past_due <= 30:
        paid_probability = 0.45
    elif days_past_due <= 60:
        paid_probability = 0.70
    elif days_past_due <= 90:
        paid_probability = 0.85
    else:
        paid_probability = 0.94

    roll = rng.random()
    if roll < paid_probability:
        return "paid"
    if roll < paid_probability + 0.01:
        return "void"
    if roll < paid_probability + 0.03:
        return "on_hold"
    return "open"


def _misspell(name: str, rng: random.Random) -> str:
    """Introduce a realistic typo into the longest word of a vendor name.

    Targets the longest word because that is the distinctive part of the name,
    the part that survives suffix stripping and therefore the part the fuzzy
    match actually compares.
    """
    words = name.split()

    # Skip legal suffixes. A typo in "Corporation" is invisible to the fuzzy
    # rule, because normalization strips that word from the correctly-spelled
    # name while the misspelled "Corporaion" survives, leaving two keys of very
    # different length and a similarity well below threshold.
    suffixes = {"inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "corp", "corp.",
                "corporation", "limited", "company", "co", "co.", "sa", "s.a.",
                "plc", "gmbh"}
    # Length 4 is the floor, not 5. "Acme Corporation" has no non-suffix word of
    # 5+ characters, and the old fallback appended " Holdings" — which the
    # normalizer strips as a legal suffix, collapsing the typo back onto the
    # original. That silently produced no second vendor record at all, so
    # DUP_FUZZY_VEND could never fire for short-named vendors.
    candidates = [i for i, w in enumerate(words)
                  if w.lower().strip(",.") not in suffixes and len(w) >= 4]
    if not candidates:
        candidates = [i for i, w in enumerate(words) if len(w) >= 4] or [0]

    target = max(candidates, key=lambda i: len(words[i]))
    word = words[target]

    # Only insertions and deletions. Transposition destroys three trigrams at
    # once, which drops short names like "Nestle" under the threshold.
    position = rng.randrange(2, len(word) - 1)
    # On a short core, a deletion costs proportionally more trigrams than an
    # insertion ("Nestle" -> "Nesle" scores 0.44), so short names only ever get
    # a doubled letter.
    if len(word) >= 8 and rng.random() < 0.5:
        word = word[:position] + word[position + 1:]       # dropped letter
    else:
        word = word[:position] + word[position] + word[position:]  # doubled letter

    words[target] = word
    return " ".join(words)


def _build_invoice(rng: random.Random, seq: int, today: date) -> dict:
    """One invoice, as a dict of clean values, before dirt is applied."""
    canonical, variants = rng.choice(VENDOR_POOL)
    vendor_display = rng.choice(variants)
    terms = rng.choice([15, 30, 30, 30, 45, 60, 90])
    invoice_date = today - timedelta(days=rng.randint(0, 220))

    n_lines = rng.choices([1, 2, 3, 4], weights=[55, 25, 13, 7])[0]
    lines = []
    for line_no in range(1, n_lines + 1):
        quantity = Decimal(rng.choice(["1", "1", "2", "3", "5", "8", "10", "40"]))
        # Draw the line value first, then back out a unit price, then recompute
        # the total from the rounded unit price. Recomputing matters: if the
        # header summed the pre-rounding values, LINE_MISMATCH would fire on
        # every multi-line invoice from rounding drift alone.
        target = _line_amount(rng)
        unit_price = (target / quantity).quantize(Decimal("0.0001"))
        if unit_price <= 0:
            unit_price = Decimal("0.0100")
        lines.append({
            "line_number": line_no,
            "line_description": rng.choice(LINE_DESCRIPTIONS),
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": (quantity * unit_price).quantize(Decimal("0.01")),
        })

    total = sum((ln["line_total"] for ln in lines), Decimal("0.00"))
    # A small share are credit memos.
    if rng.random() < 0.03:
        total = -total
        for ln in lines:
            ln["line_total"] = -ln["line_total"]

    due = invoice_date + timedelta(days=terms)
    return {
        "invoice_number": f"INV-{seq:05d}",
        "status": _payment_status(rng, due, today),
        "vendor_name": vendor_display,
        "canonical_vendor": canonical,
        "tax_id": f"{abs(hash(canonical)) % 90 + 10}-{abs(hash(canonical)) % 9000000 + 1000000}",
        "invoice_date": invoice_date,
        "due_date": due,
        "payment_terms_days": terms,
        "amount": total,
        "currency": "USD",
        "gl_code": rng.choice(GL_CODES),
        "lines": lines,
    }


def _rows_for(invoice: dict, rng: random.Random) -> list[dict]:
    """Flatten one invoice to CSV rows and apply per-row dirt."""
    date_fmt = rng.choice(DATE_FORMATTERS)

    # ~8% of rows lose their due date; the pipeline re-derives it from terms.
    due_value = "" if rng.random() < 0.08 else date_fmt(invoice["due_date"])
    # ~5% lose their GL code, which MISSING_GL should flag.
    gl_value = "" if rng.random() < 0.05 else invoice["gl_code"]
    # ~1.5% arrive with an unparseable date, which belongs in quarantine.
    if rng.random() < 0.015:
        invoice_date_value = rng.choice(["N/A", "PENDING", "00/00/0000", ""])
    else:
        invoice_date_value = date_fmt(invoice["invoice_date"])
    # Stray whitespace on roughly a fifth of vendor names.
    vendor_value = invoice["vendor_name"]
    if rng.random() < 0.20:
        vendor_value = f"  {vendor_value} "

    rows = []
    for line in invoice["lines"]:
        rows.append({
            "invoice_number": invoice["invoice_number"],
            "vendor_name": vendor_value,
            "tax_id": invoice["tax_id"],
            "invoice_date": invoice_date_value,
            "due_date": due_value,
            "payment_terms_days": invoice["payment_terms_days"],
            "amount": _format_amount(invoice["amount"], rng),
            "currency": invoice["currency"],
            "status": invoice["status"],
            "gl_code": gl_value,
            "line_number": line["line_number"],
            "line_description": line["line_description"],
            "quantity": str(line["quantity"]),
            "unit_price": str(line["unit_price"]),
            "line_total": _format_amount(line["line_total"], rng),
        })
    return rows


def generate(count: int = 3000, seed: int = 42, today: date | None = None) -> list[dict]:
    """Build `count` invoices plus a seeded population of duplicates."""
    rng = random.Random(seed)
    today = today or date.today()

    invoices = [_build_invoice(rng, seq, today) for seq in range(1, count + 1)]
    planted = {"exact": 0, "near": 0, "fuzzy": 0}

    # ~2% exact duplicates: same vendor and number, formatted differently.
    for original in rng.sample(invoices, k=max(1, int(count * 0.02))):
        copy = dict(original)
        copy["lines"] = [dict(ln) for ln in original["lines"]]
        digits = original["invoice_number"].split("-")[1]
        copy["invoice_number"] = rng.choice([
            f"INV{digits}", f"inv-{digits}", f"INV-{digits.lstrip('0')}",
        ])
        invoices.append(copy)
        planted["exact"] += 1

    # ~1.5% near duplicates: same vendor and amount, a few days apart,
    # under a genuinely different invoice number.
    for original in rng.sample(invoices[:count], k=max(1, int(count * 0.015))):
        copy = dict(original)
        copy["lines"] = [dict(ln) for ln in original["lines"]]
        copy["invoice_number"] = f"RB-{rng.randrange(10000, 99999)}"
        shift = rng.randint(1, 4)
        copy["invoice_date"] = original["invoice_date"] + timedelta(days=shift)
        copy["due_date"] = original["due_date"] + timedelta(days=shift)
        invoices.append(copy)
        planted["near"] += 1

    # ~0.5% billed twice under a misspelling of the same vendor: the classic
    # vendor-master problem, where a typo creates a second payee record.
    #
    # The typo goes in the CORE name, not the suffix. Normalization strips legal
    # suffixes, so "Acme Corp" vs "Acme Corporatoin" both reduce to "acme" and
    # the fuzzy rule has nothing to compare. A typo in the core name leaves two
    # long, near-identical keys ("vandelay industries" vs "vandelay industres")
    # whose trigram similarity clears the 0.55 threshold in 003_rules.sql.
    for original in rng.sample(invoices[:count], k=max(1, int(count * 0.005))):
        copy = dict(original)
        copy["lines"] = [dict(ln) for ln in original["lines"]]
        copy["vendor_name"] = _misspell(original["canonical_vendor"], rng)
        copy["invoice_number"] = f"S-{rng.randrange(10000, 99999)}"
        invoices.append(copy)
        planted["fuzzy"] += 1

    rng.shuffle(invoices)
    print(
        f"Planted duplicates -> exact: {planted['exact']}, "
        f"near: {planted['near']}, fuzzy-vendor: {planted['fuzzy']}"
    )
    return invoices


def write_csv(invoices: list[dict], path: Path, seed: int = 42) -> int:
    """Flatten invoices to CSV. Returns the number of rows written."""
    rng = random.Random(seed + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for invoice in invoices:
            for row in _rows_for(invoice, rng):
                writer.writerow(row)
                written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic invoice CSVs.")
    parser.add_argument("--count", type=int, default=3000, help="base invoice count")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument(
        "--out", type=Path, default=Path("data/raw/invoices_sample.csv")
    )
    args = parser.parse_args()

    invoices = generate(count=args.count, seed=args.seed)
    rows = write_csv(invoices, args.out, seed=args.seed)
    print(f"Wrote {rows} rows across {len(invoices)} invoices to {args.out}")


if __name__ == "__main__":
    main()
