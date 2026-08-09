"""Property tests for the synthetic data generator.

The CI pipeline asserts that DUP_EXACT, DUP_NEAR and the rest produce findings.
Those assertions are only meaningful if the generated data actually contains
the duplicates they look for -- and, critically, if those duplicates survive
the cleaning stage rather than being quarantined on the way through.

A generator change that stopped planting detectable duplicates would leave the
SQL rules technically correct and completely unexercised, with CI still green.
These tests are what prevent that.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ap.clean import clean_frame, normalize_vendor_name, split_invoices
from ap.generate import VENDOR_POOL, _misspell, generate, write_csv

SEED = 42
FIXED_TODAY = date(2026, 6, 1)
SIMILARITY_THRESHOLD = 0.55  # must match sql/003_rules.sql


def trigrams(text: str) -> set[str]:
    """Approximation of pg_trgm's word-boundary padded trigram set."""
    out = set()
    for word in text.split():
        padded = f"  {word} "
        out.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return out


def similarity(left: str, right: str) -> float:
    a, b = trigrams(left), trigrams(right)
    return len(a & b) / len(a | b) if (a | b) else 0.0


@pytest.fixture(scope="module")
def cleaned(tmp_path_factory):
    """Generate, write, and clean one dataset shared across the module."""
    path = tmp_path_factory.mktemp("data") / "sample.csv"
    invoices = generate(count=400, seed=SEED, today=FIXED_TODAY)
    write_csv(invoices, path, seed=SEED)
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    clean, rejects = clean_frame(raw, "sample.csv")
    headers, lines = split_invoices(clean)
    return {"headers": headers, "lines": lines, "rejects": rejects, "raw": raw}


class TestGeneratedShape:
    def test_produces_rows(self, cleaned):
        assert len(cleaned["raw"]) > 0
        assert len(cleaned["headers"]) > 0

    def test_every_invoice_has_at_least_one_line(self, cleaned):
        assert len(cleaned["lines"]) >= len(cleaned["headers"])

    def test_most_rows_survive_cleaning(self, cleaned):
        # Dirt is intentional but should stay a minority; a generator change
        # that made most rows unparseable would gut the dataset silently.
        kept = len(cleaned["lines"]) / len(cleaned["raw"])
        assert kept > 0.90, f"only {kept:.0%} of rows survived cleaning"

    def test_is_deterministic_for_a_fixed_seed(self):
        first = generate(count=50, seed=7, today=FIXED_TODAY)
        second = generate(count=50, seed=7, today=FIXED_TODAY)
        assert [i["invoice_number"] for i in first] == \
               [i["invoice_number"] for i in second]


class TestPlantedDuplicates:
    def test_exact_duplicates_survive_to_be_detectable(self, cleaned):
        # Same vendor and normalized invoice number, different raw number.
        # This is precisely what DUP_EXACT joins on.
        headers = cleaned["headers"]
        groups = headers.groupby(
            ["vendor_normalized", "invoice_number_normalized"]
        ).size()
        assert (groups > 1).sum() > 0, "no DUP_EXACT candidates in the data"

    def test_near_duplicates_survive_to_be_detectable(self, cleaned):
        # Same vendor, same amount, invoice dates within 5 days, different
        # normalized number. The DUP_NEAR join, expressed in pandas.
        headers = cleaned["headers"]
        found = 0
        for _, group in headers.groupby(["vendor_normalized", "amount"]):
            if len(group) < 2:
                continue
            records = group.to_dict("records")
            for i, a in enumerate(records):
                for b in records[i + 1:]:
                    close = abs(a["invoice_date"] - b["invoice_date"]) \
                        <= timedelta(days=5)
                    different = (a["invoice_number_normalized"]
                                 != b["invoice_number_normalized"])
                    if close and different:
                        found += 1
        assert found > 0, "no DUP_NEAR candidates in the data"

    def test_fuzzy_vendor_pairs_clear_the_similarity_threshold(self, cleaned):
        # Every distinct vendor key should have at least one near-twin, or the
        # DUP_FUZZY_VEND rule has nothing to join against.
        keys = sorted(cleaned["headers"]["vendor_normalized"].unique())
        pairs = [
            (a, b) for i, a in enumerate(keys) for b in keys[i + 1:]
            if similarity(a, b) >= SIMILARITY_THRESHOLD
        ]
        assert pairs, "no vendor pairs clear the fuzzy-match threshold"

    def test_overdue_invoices_exist(self, cleaned):
        overdue = (cleaned["headers"]["due_date"] < FIXED_TODAY).sum()
        assert overdue > 0, "no past-due invoices to age"

    def test_missing_gl_codes_exist(self, cleaned):
        assert cleaned["headers"]["gl_code"].isna().sum() > 0

    def test_quarantine_is_exercised_but_not_dominant(self, cleaned):
        rejects = cleaned["rejects"]
        assert not rejects.empty, "no rows exercise the quarantine path"
        assert len(rejects) < len(cleaned["raw"]) * 0.10


class TestMisspell:
    @pytest.mark.parametrize("canonical", [name for name, _ in VENDOR_POOL])
    def test_typos_stay_above_the_similarity_threshold(self, canonical):
        # If a typo drops below the threshold, DUP_FUZZY_VEND silently stops
        # firing. This pins the relationship between the generator and the
        # rule's tuning constant.
        import random
        for seed in range(5):
            typo = _misspell(canonical, random.Random(seed))
            score = similarity(
                normalize_vendor_name(canonical), normalize_vendor_name(typo)
            )
            assert score >= SIMILARITY_THRESHOLD, (
                f"{canonical!r} -> {typo!r} scored {score:.2f}"
            )

    @pytest.mark.parametrize("canonical", [name for name, _ in VENDOR_POOL])
    def test_typo_actually_changes_the_name(self, canonical):
        import random
        typo = _misspell(canonical, random.Random(0))
        assert normalize_vendor_name(typo) != normalize_vendor_name(canonical)
