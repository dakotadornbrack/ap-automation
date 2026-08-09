"""Unit tests for the loader's pure row-building. No database required.

These cover the boundary between pandas and psycopg2, which is where quiet type
bugs live: numpy integers that psycopg2 cannot adapt, NaN reaching a NOT NULL
column, and rows silently dropped because a key lookup missed.
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ap.load import build_invoice_rows, build_line_rows, sql_null


def header_record(**overrides) -> dict:
    row = {
        "vendor_name": "Acme Corp", "vendor_normalized": "acme",
        "tax_id": "11-1111111", "payment_terms_days": 30,
        "invoice_number": "INV-001", "invoice_number_normalized": "inv1",
        "invoice_date": date(2024, 3, 1), "due_date": date(2024, 3, 31),
        "amount": Decimal("100.00"), "currency": "USD", "status": "open", "gl_code": "5010",
        "source_file": "test.csv", "source_row": 2,
    }
    row.update(overrides)
    return row


def line_record(**overrides) -> dict:
    row = {
        "vendor_normalized": "acme", "invoice_number": "INV-001",
        "line_number": 1, "line_description": "Services",
        "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
        "line_total": Decimal("100.00"), "gl_code": "5010",
    }
    row.update(overrides)
    return row


# Frames are built from a *list of dicts*, matching how clean_frame constructs
# its output. This is not incidental: pandas infers a column's dtype across all
# rows, so a gl_code column holding both strings and Nones becomes the `str`
# dtype and silently rewrites those Nones as NaN. A single-row frame keeps the
# None as a None and would not reproduce the bug these tests exist to pin.
def header_frame(**overrides) -> pd.DataFrame:
    return pd.DataFrame([header_record(**overrides)])


def line_frame(**overrides) -> pd.DataFrame:
    return pd.DataFrame([line_record(**overrides)])


class TestBuildInvoiceRows:
    def test_builds_one_tuple_per_header(self):
        rows = build_invoice_rows(header_frame(), {"acme": 7})
        assert len(rows) == 1
        assert rows[0][0] == 7
        assert rows[0][1] == "INV-001"

    def test_unknown_vendor_is_dropped_not_raised(self):
        assert build_invoice_rows(header_frame(), {}) == []

    def test_tuple_matches_the_sql_column_order(self):
        # The insert names ten columns positionally. If someone adds a column
        # to the SQL without updating this tuple (or reorders either), Postgres
        # will happily write invoice_date into due_date or fail with a type
        # error at runtime. Asserting the full shape here catches that in CI.
        #
        # (The earlier version of this test asserted source_row was a Python
        # int rather than numpy.int64. That is worth coercing for pandas 2.x,
        # where to_dict yields numpy scalars, but pandas 3 unboxes them
        # unconditionally -- so the assertion could not fail and was removed
        # rather than left as decoration.)
        rows = build_invoice_rows(header_frame(), {"acme": 7})
        assert rows[0] == (
            7,                      # vendor_id
            "INV-001",              # invoice_number
            date(2024, 3, 1),       # invoice_date
            date(2024, 3, 31),      # due_date
            Decimal("100.00"),      # amount
            "USD",                  # currency
            "open",                 # status
            "5010",                 # gl_code
            "test.csv",             # source_file
            2,                      # source_row
        )

    def test_amount_stays_decimal_through_the_boundary(self):
        rows = build_invoice_rows(header_frame(), {"acme": 1})
        assert isinstance(rows[0][4], Decimal)
        assert rows[0][4] == Decimal("100.00")

    def test_dates_stay_dates(self):
        rows = build_invoice_rows(header_frame(), {"acme": 1})
        assert rows[0][2] == date(2024, 3, 1)
        assert rows[0][3] == date(2024, 3, 31)

    def test_absent_gl_code_becomes_sql_null(self):
        # Regression, and the reason MISSING_GL reported nothing on the first
        # real run against Postgres. clean_frame emits None for an absent
        # gl_code, but a column mixing strings with Nones takes pandas' `str`
        # dtype, which stores those Nones as NaN. psycopg2 adapts NaN to
        # 'NaN'::float, and Postgres assignment-casts that into the TEXT column
        # as the four-character string 'NaN' -- so the invoice loads with a GL
        # code that looks present, MISSING_GL matches nothing, and no error is
        # raised anywhere. The only symptom is a control quietly finding zero.
        frame = pd.DataFrame([
            header_record(),
            header_record(invoice_number="INV-002", gl_code=None),
        ])
        rows = build_invoice_rows(frame, {"acme": 7})
        assert rows[0][7] == "5010"
        assert rows[1][7] is None

    def test_nan_gl_code_becomes_sql_null(self):
        # The same assertion against an explicit NaN, so the test still pins the
        # behaviour if a future pandas changes its dtype inference.
        rows = build_invoice_rows(header_frame(gl_code=float("nan")), {"acme": 7})
        assert rows[0][7] is None

    def test_only_known_vendors_survive_a_mixed_batch(self):
        frame = pd.concat(
            [header_frame(), header_frame(vendor_normalized="globex",
                                          invoice_number="INV-002")],
            ignore_index=True,
        )
        rows = build_invoice_rows(frame, {"acme": 1})
        assert len(rows) == 1
        assert rows[0][1] == "INV-001"


class TestBuildLineRows:
    def test_links_line_to_its_invoice_id(self):
        rows = build_line_rows(line_frame(), {"acme": 7}, {(7, "INV-001"): 99})
        assert len(rows) == 1
        assert rows[0][0] == 99

    def test_line_without_a_resolved_invoice_is_dropped(self):
        assert build_line_rows(line_frame(), {"acme": 7}, {}) == []

    def test_line_without_a_total_is_dropped(self):
        rows = build_line_rows(
            line_frame(line_total=None), {"acme": 7}, {(7, "INV-001"): 99}
        )
        assert rows == []

    def test_missing_unit_price_falls_back_to_the_total(self):
        rows = build_line_rows(
            line_frame(unit_price=None), {"acme": 7}, {(7, "INV-001"): 99}
        )
        assert rows[0][4] == Decimal("100.00")

    def test_tuple_matches_the_sql_column_order(self):
        rows = build_line_rows(line_frame(), {"acme": 7}, {(7, "INV-001"): 99})
        assert rows[0] == (
            99,                     # invoice_id
            1,                      # line_number
            "Services",             # description
            Decimal("1"),           # quantity
            Decimal("100.00"),      # unit_price
            Decimal("100.00"),      # line_total
            "5010",                 # gl_code
        )

    def test_absent_line_fields_become_sql_null(self):
        # Same NaN-to-'NaN' hazard as on the header. It is worse on a line:
        # quantity and unit_price are NUMERIC, and Postgres accepts a NaN there
        # natively, so a corrupted line total would poison the LINE_MISMATCH
        # reconciliation rather than merely disabling a rule.
        frame = pd.DataFrame([
            line_record(line_number=1),
            line_record(line_number=2, line_description=None, gl_code=None),
        ])
        rows = build_line_rows(frame, {"acme": 7}, {(7, "INV-001"): 99})
        assert rows[1][2] is None   # description
        assert rows[1][6] is None   # gl_code

    def test_real_values_are_not_nulled_by_the_coercion(self):
        # Guards the other direction: sql_null must leave Decimal, str and int
        # alone. pd.isna raises or misbehaves on some types, and a helper that
        # over-reaches would blank out live money.
        assert sql_null(Decimal("100.00")) == Decimal("100.00")
        assert sql_null("5010") == "5010"
        assert sql_null(0) == 0
        assert sql_null("") == ""

    @pytest.mark.parametrize("bad_invoice", ["INV-999", "inv-001", ""])
    def test_invoice_number_lookup_is_exact_not_normalized(self, bad_invoice):
        # The id map is keyed on the raw number, matching the DB constraint.
        # A normalized lookup here would attach lines to the wrong copy of a
        # duplicate pair.
        rows = build_line_rows(
            line_frame(invoice_number=bad_invoice),
            {"acme": 7},
            {(7, "INV-001"): 99},
        )
        assert rows == []

    def test_multiple_lines_all_link_to_the_same_invoice(self):
        frame = pd.concat(
            [line_frame(line_number=1), line_frame(line_number=2)],
            ignore_index=True,
        )
        rows = build_line_rows(frame, {"acme": 7}, {(7, "INV-001"): 99})
        assert len(rows) == 2
        assert {r[0] for r in rows} == {99}
        assert sorted(r[1] for r in rows) == [1, 2]
