"""Load cleaned invoice data into Postgres.

Idempotent throughout. Re-running the same file is a no-op rather than a second
set of invoices, which matters because an AP pipeline that double-loads is
indistinguishable from one that causes double payments.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values

from . import db
from .clean import clean_frame, split_invoices


def upsert_vendors(conn, headers: pd.DataFrame) -> dict[str, int]:
    """Insert any unseen vendors and return normalized_name -> vendor_id."""
    if headers.empty:
        return {}

    vendors = (
        headers[["vendor_name", "vendor_normalized", "payment_terms_days", "tax_id"]]
        .drop_duplicates(subset=["vendor_normalized"], keep="first")
        .to_records(index=False)
    )
    rows = [(str(n), str(k), int(t), (None if pd.isna(x) else x)) for n, k, t, x in vendors]

    with conn.cursor() as cur:
        # DO UPDATE rather than DO NOTHING so the RETURNING clause emits a row
        # for existing vendors too, giving the full id map in one round trip.
        #
        # fetch=True is load-bearing. execute_values batches into pages of 100
        # and, with fetch=False, only the final page's RETURNING rows are left
        # on the cursor -- so cur.fetchall() would silently return the last 100
        # vendors and nothing else. fetch=True collects every page.
        returned = execute_values(
            cur,
            """
            INSERT INTO vendors (name, normalized_name, payment_terms_days, tax_id)
            VALUES %s
            ON CONFLICT (normalized_name) DO UPDATE
                SET payment_terms_days = EXCLUDED.payment_terms_days
            RETURNING normalized_name, vendor_id
            """,
            rows,
            fetch=True,
        )
        return {name: vid for name, vid in returned}


def sql_null(value):
    """Map pandas' missing-value sentinels onto a real SQL NULL.

    Cleaning emits None for an absent optional field, but a round trip through a
    DataFrame turns that None into NaN. psycopg2 adapts NaN to the literal
    `'NaN'::float`, and Postgres will assignment-cast that into a TEXT column as
    the four-character string 'NaN' -- or into a NUMERIC column as an actual NaN.
    Either way an absent value arrives looking present, which is worse than an
    error: a missing GL code becomes a GL code of "NaN", MISSING_GL matches
    nothing, and the control silently stops working.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # pd.isna raises on some container types; those are never missing.
        pass
    return value


def build_invoice_rows(headers: pd.DataFrame, vendor_ids: dict[str, int]) -> list[tuple]:
    """Turn cleaned headers into insert tuples. Pure, so it is unit testable.

    Headers whose vendor is not in the id map are dropped rather than raising:
    the vendor upsert is the authority on which vendors exist, and a missing one
    means the header was already rejected upstream.
    """
    rows = []
    for record in headers.to_dict("records"):
        vendor_id = vendor_ids.get(record["vendor_normalized"])
        if vendor_id is None:
            continue
        rows.append((
            vendor_id,
            record["invoice_number"],
            record["invoice_date"],
            record["due_date"],
            record["amount"],
            record["currency"],
            record["status"],
            sql_null(record["gl_code"]),
            record["source_file"],
            int(record["source_row"]),
        ))
    return rows


def build_line_rows(
    lines: pd.DataFrame,
    vendor_ids: dict[str, int],
    invoice_ids: dict[tuple[int, str], int],
) -> list[tuple]:
    """Turn cleaned lines into insert tuples, keyed back to their header.

    A line with no parseable total is skipped: inserting it would silently
    corrupt the header-versus-lines reconciliation that LINE_MISMATCH depends
    on. When the unit price is missing but the total is present, the total
    stands in, which is correct for single-quantity lines and the best
    available estimate otherwise.
    """
    rows = []
    for record in lines.to_dict("records"):
        vendor_id = vendor_ids.get(record["vendor_normalized"])
        invoice_id = invoice_ids.get((vendor_id, record["invoice_number"]))
        if invoice_id is None:
            continue
        line_total = sql_null(record["line_total"])
        if line_total is None:
            continue
        unit_price = sql_null(record["unit_price"])
        rows.append((
            invoice_id,
            int(record["line_number"]),
            sql_null(record["line_description"]),
            sql_null(record["quantity"]),
            unit_price if unit_price is not None else line_total,
            line_total,
            sql_null(record["gl_code"]),
        ))
    return rows


def insert_invoices(conn, headers: pd.DataFrame, vendor_ids: dict[str, int]) -> dict:
    """Insert invoice headers. Returns id map and counts."""
    if headers.empty:
        return {"ids": {}, "inserted": 0, "skipped": 0}

    rows = build_invoice_rows(headers, vendor_ids)
    if not rows:
        return {"ids": {}, "inserted": 0, "skipped": 0}

    with conn.cursor() as cur:
        # fetch=True for the same reason as upsert_vendors: without it only the
        # final page of 100 RETURNING rows survives on the cursor.
        returned = execute_values(
            cur,
            """
            INSERT INTO invoices (
                vendor_id, invoice_number, invoice_date, due_date,
                amount, currency, status, gl_code, source_file, source_row
            )
            VALUES %s
            ON CONFLICT (vendor_id, invoice_number) DO NOTHING
            RETURNING vendor_id, invoice_number, invoice_id
            """,
            rows,
            fetch=True,
        )
        newly_inserted = {(v, n): i for v, n, i in returned}

        # DO NOTHING returns nothing for rows that conflicted, so their ids are
        # still needed or their lines would be orphaned. Resolve the whole batch
        # in one pass against a temp table rather than a giant IN list.
        cur.execute(
            "CREATE TEMP TABLE _batch (vendor_id INT, invoice_number TEXT) "
            "ON COMMIT DROP"
        )
        execute_values(
            cur,
            "INSERT INTO _batch (vendor_id, invoice_number) VALUES %s",
            [(r[0], r[1]) for r in rows],
        )
        cur.execute(
            """
            SELECT i.vendor_id, i.invoice_number, i.invoice_id
            FROM invoices i
            JOIN _batch b
              ON b.vendor_id = i.vendor_id
             AND b.invoice_number = i.invoice_number
            """
        )
        all_ids = {(v, n): i for v, n, i in cur.fetchall()}
        cur.execute("DROP TABLE _batch")

    return {
        "ids": all_ids,
        "inserted": len(newly_inserted),
        "skipped": len(all_ids) - len(newly_inserted),
    }


def insert_lines(conn, lines: pd.DataFrame, vendor_ids: dict[str, int],
                 invoice_ids: dict) -> int:
    """Insert invoice lines, keyed back to their header."""
    if lines.empty:
        return 0

    rows = build_line_rows(lines, vendor_ids, invoice_ids)
    if not rows:
        return 0

    with conn.cursor() as cur:
        # cur.rowcount would report only the last page of 100 here. RETURNING
        # with fetch=True gives an accurate count of rows that actually landed,
        # which is not the same as len(rows) once DO NOTHING skips conflicts.
        returned = execute_values(
            cur,
            """
            INSERT INTO invoice_lines (
                invoice_id, line_number, description, quantity,
                unit_price, line_total, gl_code
            )
            VALUES %s
            ON CONFLICT (invoice_id, line_number) DO NOTHING
            RETURNING line_id
            """,
            rows,
            fetch=True,
        )
        return len(returned)


def ingest_file(conn, path: Path, force: bool = False) -> dict:
    """Clean and load one CSV. Returns a summary dict."""
    hash_value = db.file_hash(path)
    if not force and db.already_ingested(conn, hash_value):
        return {"source_file": path.name, "status": "skipped_already_ingested"}

    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    clean, rejects = clean_frame(raw, path.name)
    headers, lines = split_invoices(clean)

    if not rejects.empty:
        quarantine = Path("data/quarantine") / f"{path.stem}_rejects.csv"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        rejects.to_csv(quarantine, index=False)

    vendor_ids = upsert_vendors(conn, headers)
    invoice_result = insert_invoices(conn, headers, vendor_ids)
    line_count = insert_lines(conn, lines, vendor_ids, invoice_result["ids"])

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_runs (
                source_file, file_hash, rows_read, rows_loaded, rows_rejected, finished_at
            )
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (file_hash) DO UPDATE SET finished_at = now()
            """,
            (path.name, hash_value, len(raw), invoice_result["inserted"], len(rejects)),
        )

    return {
        "source_file": path.name,
        "status": "loaded",
        "rows_read": len(raw),
        "invoices_inserted": invoice_result["inserted"],
        "invoices_skipped": invoice_result["skipped"],
        "lines_inserted": line_count,
        "rows_rejected": len(rejects),
        "vendors": len(vendor_ids),
    }
