"""Rule-engine behaviour that only shows up across runs.

Most rules can be judged from a single execution. The overdue tiers cannot: the
CASE in 003_rules.sql picks exactly one tier per invoice, so a single run always
looks correct. The defect appears only when the engine runs again after an
invoice has aged past the next threshold, because OVERDUE_30 and OVERDUE_60 are
different rule_codes and the unique constraint treats them as unrelated
findings. The invoice then carries two open exceptions and a clerk reviews it
twice.

These tests build their own invoice rather than relying on loaded data -- CI
runs pytest before the pipeline, so the tables are empty at this point -- and
roll back, so they leave no trace in the database they run against.

Requires a database; skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg2
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires a live Postgres (set DATABASE_URL)",
)


@pytest.fixture()
def conn():
    from ap import db
    connection = psycopg2.connect(db.dsn())
    db.migrate(connection)
    try:
        yield connection
    finally:
        # Everything these tests write is rolled back, including the findings
        # the rule engine produces for rows that happen to already be loaded.
        connection.rollback()
        connection.close()


def make_overdue_invoice(conn, days_past_due: int) -> int:
    """Insert one open invoice that is exactly `days_past_due` days late."""
    due = date.today() - timedelta(days=days_past_due)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vendors (name, normalized_name) VALUES (%s, %s) "
            "ON CONFLICT (normalized_name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING vendor_id",
            ("Tier Test Vendor", f"tiertest{days_past_due}"),
        )
        vendor_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO invoices (vendor_id, invoice_number, invoice_date, due_date,"
            " amount, status, gl_code, source_file)"
            " VALUES (%s, %s, %s, %s, %s, 'open', '5010', 'tiertest.csv')"
            " RETURNING invoice_id",
            (vendor_id, f"TIER-{days_past_due}", due - timedelta(days=30), due, 500),
        )
        return cur.fetchone()[0]


def open_overdue_rows(conn, invoice_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rule_code FROM exceptions"
            " WHERE invoice_id = %s AND resolved_at IS NULL"
            "   AND rule_code IN ('OVERDUE_30', 'OVERDUE_60', 'OVERDUE_90')"
            " ORDER BY rule_code",
            (invoice_id,),
        )
        return [row[0] for row in cur.fetchall()]


def run_rules(conn) -> None:
    from ap import db
    db.run_sql_file(conn, db.SQL_DIR / "003_rules.sql")


def test_aging_past_a_threshold_moves_the_tier_instead_of_adding_one(conn):
    # An invoice 64 days late, already carrying the OVERDUE_30 finding it earned
    # a month ago. Re-running the engine must retire that row, not add to it.
    invoice_id = make_overdue_invoice(conn, days_past_due=64)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO exceptions (invoice_id, rule_code, severity, detail)"
            " VALUES (%s, 'OVERDUE_30', 'warn', '35 days past due')",
            (invoice_id,),
        )

    run_rules(conn)

    assert open_overdue_rows(conn, invoice_id) == ["OVERDUE_60"]


def test_repeated_runs_do_not_accumulate_findings(conn):
    invoice_id = make_overdue_invoice(conn, days_past_due=95)
    for _ in range(3):
        run_rules(conn)
    assert open_overdue_rows(conn, invoice_id) == ["OVERDUE_90"]


def test_a_resolved_finding_survives_the_tier_change(conn):
    # Retiring a stale tier must not rewrite findings someone already signed
    # off on. A reviewer's decision is a record, not scratch space.
    invoice_id = make_overdue_invoice(conn, days_past_due=64)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO exceptions (invoice_id, rule_code, severity, detail, resolved_at)"
            " VALUES (%s, 'OVERDUE_30', 'warn', 'reviewed and cleared', now())",
            (invoice_id,),
        )

    run_rules(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM exceptions"
            " WHERE invoice_id = %s AND rule_code = 'OVERDUE_30'"
            "   AND resolved_at IS NOT NULL",
            (invoice_id,),
        )
        assert cur.fetchone()[0] == 1, "a resolved finding was deleted"
    assert open_overdue_rows(conn, invoice_id) == ["OVERDUE_60"]
