"""Parity between the Python and SQL implementations of the same rule.

`normalize_invoice_number` exists twice: once in Python for the cleaning stage,
once in SQL for the duplicate joins. Two copies of a rule drift apart silently,
and when they do, DUP_EXACT quietly stops matching things the cleaner treated as
identical. This test is the thing that catches that.

Requires a database; skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os

import pytest

from ap.clean import normalize_invoice_number

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires a live Postgres (set DATABASE_URL)",
)

SAMPLES = [
    "INV-0042", "inv 42", "INV42", "0042", "100", "INV-1000", "2024-0100",
    "RB-00007", "s-99999", "0", "000", "A0B0C1", "  INV-0001  ", "INV/0007",
]


@pytest.fixture(scope="module")
def conn():
    from ap import db
    with db.connect() as connection:
        db.migrate(connection)
        # 003_rules.sql defines the function; run it so the function exists.
        db.run_sql_file(connection, db.SQL_DIR / "003_rules.sql")
        yield connection


@pytest.mark.parametrize("value", SAMPLES)
def test_python_and_sql_normalizers_agree(conn, value):
    with conn.cursor() as cur:
        cur.execute("SELECT normalize_invoice_number(%s)", (value,))
        sql_result = cur.fetchone()[0] or ""
    assert sql_result == normalize_invoice_number(value), (
        f"drift on {value!r}: SQL gave {sql_result!r}, "
        f"Python gave {normalize_invoice_number(value)!r}"
    )
