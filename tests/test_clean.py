"""Unit tests for the cleaning layer. No database required."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ap.clean import (
    clean_frame,
    normalize_invoice_number,
    normalize_vendor_name,
    parse_amount,
    parse_date,
    resolve_due_date,
    split_invoices,
)


class TestNormalizeVendorName:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("Acme Corporation", "ACME Corp."),
            ("Acme Corporation", "  acme   corp  "),
            ("Globex LLC", "Globex L.L.C."),
            ("Nestle SA", "Nestlé S.A."),
            ("Smith & Sons Ltd", "Smith and Sons Limited"),
            ("Initech", "Initech, Inc."),
        ],
    )
    def test_variants_collapse_to_same_key(self, left, right):
        assert normalize_vendor_name(left) == normalize_vendor_name(right)

    def test_distinct_vendors_stay_distinct(self):
        assert normalize_vendor_name("Acme Corp") != normalize_vendor_name("Globex LLC")

    def test_suffix_only_name_is_not_emptied_of_meaning(self):
        # "Initech" must survive; only the suffix is stripped.
        assert normalize_vendor_name("Initech Inc") == "initech"

    @pytest.mark.parametrize("value", [None, "", "   ", float("nan")])
    def test_empty_inputs_yield_empty_string(self, value):
        assert normalize_vendor_name(value) == ""


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("$1,234.56", Decimal("1234.56")),
            ("1234.5", Decimal("1234.50")),
            ("$0.01", Decimal("0.01")),
            ("(450.00)", Decimal("-450.00")),
            ("450.00-", Decimal("-450.00")),
            ("450.00CR", Decimal("-450.00")),
            ("-12.30", Decimal("-12.30")),
            ("£99.99", Decimal("99.99")),
        ],
    )
    def test_parses_real_world_formats(self, raw, expected):
        assert parse_amount(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "n/a", "PENDING", "$"])
    def test_unparseable_returns_none(self, raw):
        assert parse_amount(raw) is None

    def test_returns_decimal_not_float(self):
        assert isinstance(parse_amount("0.10"), Decimal)

    def test_no_float_drift_on_repeated_addition(self):
        # The reason money is Decimal: this sum is exact, 0.1 * 3 in float is not.
        total = sum((parse_amount("0.10") for _ in range(3)), Decimal("0"))
        assert total == Decimal("0.30")

    def test_rounds_to_cents(self):
        assert parse_amount("99.999") == Decimal("100.00")


class TestParseDate:
    @pytest.mark.parametrize(
        "raw",
        ["2024-03-15", "2024/03/15", "03/15/2024", "15-Mar-2024",
         "Mar 15, 2024", "March 15, 2024", "20240315", "15.03.2024"],
    )
    def test_accepts_known_formats(self, raw):
        assert parse_date(raw) == date(2024, 3, 15)

    @pytest.mark.parametrize("raw", [None, "", "garbage", "00/00/0000", "PENDING"])
    def test_rejects_unparseable(self, raw):
        assert parse_date(raw) is None

    def test_ambiguous_dates_resolve_month_first(self):
        # Documented choice: US-style. Asserted so a change is deliberate.
        assert parse_date("03/04/2024") == date(2024, 3, 4)


class TestNormalizeInvoiceNumber:
    @pytest.mark.parametrize("raw", ["INV-0042", "inv 42", "INV42", "INV_0042"])
    def test_formatting_variants_collapse(self, raw):
        assert normalize_invoice_number(raw) == "inv42"

    def test_leading_zeros_stripped_from_bare_numbers(self):
        assert normalize_invoice_number("0042") == "42"

    def test_internal_zeros_preserved(self):
        assert normalize_invoice_number("100") == "100"
        assert normalize_invoice_number("INV-1000") == "inv1000"


class TestResolveDueDate:
    def test_uses_stated_due_date_when_present(self):
        assert resolve_due_date(date(2024, 3, 1), date(2024, 4, 1), 45) == date(2024, 4, 1)

    def test_derives_from_terms_when_missing(self):
        assert resolve_due_date(date(2024, 3, 1), None, 45) == date(2024, 4, 15)

    def test_defaults_to_net_30_without_terms(self):
        assert resolve_due_date(date(2024, 3, 1), None, None) == date(2024, 3, 31)

    def test_none_without_an_invoice_date(self):
        assert resolve_due_date(None, None, 30) is None


def _frame(**overrides) -> pd.DataFrame:
    row = {
        "invoice_number": "INV-001", "vendor_name": "Acme Corp",
        "tax_id": "11-1111111", "invoice_date": "2024-03-01",
        "due_date": "2024-03-31", "payment_terms_days": "30",
        "amount": "100.00", "currency": "USD", "status": "open", "gl_code": "5010",
        "line_number": "1", "line_description": "Services",
        "quantity": "1", "unit_price": "100.00", "line_total": "100.00",
    }
    row.update(overrides)
    return pd.DataFrame([row])


class TestCleanFrame:
    def test_good_row_survives(self):
        clean, rejects = clean_frame(_frame(), "test.csv")
        assert len(clean) == 1 and rejects.empty
        assert clean.iloc[0]["amount"] == Decimal("100.00")

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"vendor_name": ""}, "missing_vendor"),
            ({"invoice_number": ""}, "missing_invoice_number"),
            ({"invoice_date": "garbage"}, "unparseable_invoice_date"),
            ({"amount": "n/a"}, "unparseable_amount"),
            ({"amount": "0.00"}, "zero_amount"),
            ({"due_date": "2024-01-01"}, "due_date_before_invoice_date"),
        ],
    )
    def test_bad_rows_quarantined_with_reason(self, overrides, reason):
        clean, rejects = clean_frame(_frame(**overrides), "test.csv")
        assert clean.empty
        assert rejects.iloc[0]["reject_reason"] == reason

    def test_rejects_keep_original_values_for_resubmission(self):
        clean, rejects = clean_frame(_frame(amount="n/a"), "test.csv")
        assert rejects.iloc[0]["amount"] == "n/a"
        assert rejects.iloc[0]["invoice_number"] == "INV-001"

    def test_missing_due_date_is_derived_not_rejected(self):
        clean, rejects = clean_frame(
            _frame(due_date="", payment_terms_days="45"), "test.csv"
        )
        assert rejects.empty
        assert clean.iloc[0]["due_date"] == date(2024, 4, 15)

    def test_source_row_numbers_account_for_the_header(self):
        clean, _ = clean_frame(_frame(), "test.csv")
        assert clean.iloc[0]["source_row"] == 2

    def test_repeated_line_number_is_quarantined(self):
        df = pd.concat([_frame(), _frame()], ignore_index=True)
        clean, rejects = clean_frame(df, "test.csv")
        assert len(clean) == 1
        assert rejects.iloc[0]["reject_reason"] == "repeated_line_number_within_invoice"


class TestStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("open", "open"), ("PAID", "paid"), ("  Void ", "void"),
            ("on_hold", "on_hold"), ("on-hold", "on_hold"), ("On Hold", "on_hold"),
            ("settled", "paid"), ("cancelled", "void"), ("outstanding", "open"),
            ("disputed", "on_hold"), ("", "open"),
        ],
    )
    def test_synonyms_map_onto_allowed_values(self, raw, expected):
        clean, rejects = clean_frame(_frame(status=raw), "test.csv")
        assert rejects.empty
        assert clean.iloc[0]["status"] == expected

    def test_every_output_satisfies_the_schema_constraint(self):
        # If this drifts from the CHECK constraint in 001_schema.sql, the load
        # fails with an integrity error instead of a readable reject reason.
        from ap.clean import VALID_STATUSES
        for raw in ["open", "settled", "cancelled", "hold", ""]:
            clean, _ = clean_frame(_frame(status=raw), "test.csv")
            assert clean.iloc[0]["status"] in VALID_STATUSES

    def test_unknown_status_is_rejected_not_defaulted(self):
        # Defaulting to "open" would add a possibly-settled invoice to the
        # payables balance.
        clean, rejects = clean_frame(_frame(status="ZZ_WEIRD"), "test.csv")
        assert clean.empty
        assert rejects.iloc[0]["reject_reason"] == "unrecognized_status"

    def test_missing_status_column_defaults_to_open(self):
        df = _frame()
        df = df.drop(columns=["status"])
        clean, rejects = clean_frame(df, "test.csv")
        assert rejects.empty
        assert clean.iloc[0]["status"] == "open"


class TestSplitInvoices:
    def test_multiline_invoice_stays_one_header(self):
        df = pd.concat(
            [_frame(line_number="1", line_total="60.00"),
             _frame(line_number="2", line_total="40.00")],
            ignore_index=True,
        )
        clean, _ = clean_frame(df, "test.csv")
        headers, lines = split_invoices(clean)
        assert len(headers) == 1
        assert len(lines) == 2

    def test_format_variant_duplicates_survive_as_separate_invoices(self):
        # The core behaviour of the project: these two must both reach the
        # database so DUP_EXACT can report them as a pair. Collapsing them here
        # would hide the finding entirely.
        df = pd.concat(
            [_frame(invoice_number="INV-0042"), _frame(invoice_number="INV42")],
            ignore_index=True,
        )
        clean, rejects = clean_frame(df, "test.csv")
        headers, _ = split_invoices(clean)
        assert rejects.empty
        assert len(headers) == 2
        # ...but they normalize to the same key, which is what SQL joins on.
        assert headers["invoice_number_normalized"].nunique() == 1

    def test_empty_input_returns_empty_frames(self):
        headers, lines = split_invoices(pd.DataFrame())
        assert headers.empty and lines.empty
