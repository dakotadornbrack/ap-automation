"""Command-line interface.

    python -m ap.cli migrate
    python -m ap.cli ingest data/raw/*.csv
    python -m ap.cli rules
    python -m ap.cli report
    python -m ap.cli pipeline data/raw/invoices_sample.csv   # all of the above
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db
from .load import ingest_file


def cmd_migrate(_: argparse.Namespace) -> int:
    with db.connect() as conn:
        applied = db.migrate(conn)
    print("Applied:", ", ".join(applied))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"No such file: {missing[0]}", file=sys.stderr)
        return 1

    with db.connect() as conn:
        for path in paths:
            result = ingest_file(conn, path, force=args.force)
            if result["status"] == "skipped_already_ingested":
                print(f"{path.name}: already ingested, skipping (use --force to reload)")
                continue
            print(
                f"{path.name}: {result['rows_read']} rows read -> "
                f"{result['invoices_inserted']} invoices, "
                f"{result['lines_inserted']} lines, "
                f"{result['invoices_skipped']} already present, "
                f"{result['rows_rejected']} quarantined"
            )
    return 0


def cmd_rules(_: argparse.Namespace) -> int:
    with db.connect() as conn:
        counts = db.run_rules(conn)
    if not counts:
        print("No exceptions found.")
        return 0
    width = max(len(code) for code in counts)
    print("Open exceptions by rule:")
    for code, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {code:<{width}}  {count:>6}")
    print(f"  {'TOTAL':<{width}}  {sum(counts.values()):>6}")
    return 0


def cmd_report(_: argparse.Namespace) -> int:
    with db.connect() as conn:
        kpis = db.query(conn, "SELECT * FROM v_kpis")[0]
        aging = db.query(conn, "SELECT * FROM v_aging_summary")
        vendors = db.query(
            conn,
            "SELECT vendor_name, open_balance, past_due_amount "
            "FROM v_vendor_exposure ORDER BY open_balance DESC LIMIT 10",
        )

    print("\n=== KPIs ===")
    print(f"  Total open AP        {kpis['total_open_ap']:>14,.2f}")
    print(f"  Past due             {kpis['past_due_amount']:>14,.2f} "
          f"({kpis['past_due_count']} invoices)")
    print(f"  Duplicate flags      {kpis['duplicate_flags']:>14}")
    print(f"  Duplicate exposure   {kpis['duplicate_exposure']:>14,.2f}")

    print("\n=== Aging ===")
    for row in aging:
        print(f"  {row['aging_bucket']:<8} {row['invoice_count']:>5} invoices "
              f"{row['total_amount']:>14,.2f}")

    print("\n=== Top vendors by open balance ===")
    for row in vendors:
        print(f"  {row['vendor_name']:<28} {row['open_balance']:>14,.2f} "
              f"(past due {row['past_due_amount']:>12,.2f})")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    for step in (cmd_migrate, cmd_ingest, cmd_rules, cmd_report):
        code = step(args)
        if code != 0:
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ap", description="AP invoice automation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create schema and views").set_defaults(func=cmd_migrate)

    ingest = sub.add_parser("ingest", help="clean and load CSV files")
    ingest.add_argument("paths", nargs="+")
    ingest.add_argument("--force", action="store_true",
                        help="reload even if the file was ingested before")
    ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("rules", help="run the rule engine").set_defaults(func=cmd_rules)
    sub.add_parser("report", help="print a console summary").set_defaults(func=cmd_report)

    pipeline = sub.add_parser("pipeline", help="migrate, ingest, rules, report")
    pipeline.add_argument("paths", nargs="+")
    pipeline.add_argument("--force", action="store_true")
    pipeline.set_defaults(func=cmd_pipeline)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
