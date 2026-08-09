#!/usr/bin/env python3
"""Post-pipeline smoke assertions for CI.

A green unit-test run only proves the cleaning functions work. This proves the
whole thing works end to end: that data actually landed, that the duplicate
rules fired on the planted duplicates, and that the aging maths is internally
consistent. Without this, the pipeline could silently load nothing and CI would
still pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap import db  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        FAILURES.append(message)


def main() -> int:
    with db.connect() as conn:
        invoices = db.query(conn, "SELECT COUNT(*) AS n FROM invoices")[0]["n"]
        vendors = db.query(conn, "SELECT COUNT(*) AS n FROM vendors")[0]["n"]
        lines = db.query(conn, "SELECT COUNT(*) AS n FROM invoice_lines")[0]["n"]
        by_rule = {
            row["rule_code"]: row["n"]
            for row in db.query(
                conn,
                "SELECT rule_code, COUNT(*) AS n FROM exceptions "
                "WHERE resolved_at IS NULL GROUP BY rule_code",
            )
        }
        kpis = db.query(conn, "SELECT * FROM v_kpis")[0]
        aging_total = db.query(
            conn, "SELECT COALESCE(SUM(total_amount), 0) AS t FROM v_aging_summary"
        )[0]["t"]
        orphan_lines = db.query(
            conn,
            "SELECT COUNT(*) AS n FROM invoice_lines l "
            "LEFT JOIN invoices i USING (invoice_id) WHERE i.invoice_id IS NULL",
        )[0]["n"]
        self_referential = db.query(
            conn, "SELECT COUNT(*) AS n FROM exceptions WHERE invoice_id = related_id"
        )[0]["n"]

    print("Loaded:")
    check(invoices > 0, f"invoices loaded ({invoices})")
    check(vendors > 0, f"vendors loaded ({vendors})")
    check(lines >= invoices, f"lines loaded ({lines}) and at least one per invoice")
    check(orphan_lines == 0, f"no orphaned invoice lines ({orphan_lines})")

    print("\nRules fired:")
    for rule in ("DUP_EXACT", "DUP_NEAR", "OVERDUE_30", "MISSING_GL"):
        check(by_rule.get(rule, 0) > 0, f"{rule} produced findings ({by_rule.get(rule, 0)})")

    print("\nConsistency:")
    check(
        self_referential == 0,
        f"no exception cites itself as its own duplicate ({self_referential})",
    )
    check(
        abs(float(aging_total) - float(kpis["total_open_ap"])) < 0.01,
        "aging buckets sum to the total open AP KPI",
    )
    check(
        int(kpis["duplicate_flags"]) >= by_rule.get("DUP_EXACT", 0),
        "duplicate KPI covers at least the exact-duplicate findings",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} assertion(s) failed.")
        return 1
    print("\nAll assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
