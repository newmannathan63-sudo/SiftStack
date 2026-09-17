"""Regression tests for the Duval LP mortgage-preforeclosure plaintiff/case-type
filter (duval_clerk_scraper._is_mortgage_preforeclosure).

2026-09-17: a family-law divorce LP ("AKEL SUMMAR L" v. "AKEL NADER A"/"AKEL
AKEL J", case 16-2022-DR-003389-FMXX-MA) and a mechanic's lien from "ELO
RESTORATION LLC" both slipped through the plaintiff-name denylist and were
uploaded to DataSift as if they were real mortgage foreclosures. Fixed by
adding a case-type allowlist (only "CA"/"CC" case numbers pass) checked
post-document-fetch, plus adding "RESTORATION" to the contractor keyword
list. These tests pin both fixes and the real historical records they must
not regress.

Run: .venv/Scripts/python.exe tests/test_lp_plaintiff_filter.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from duval_clerk_scraper import _is_mortgage_preforeclosure


def test_divorce_case_rejected():
    # Real case from 2026-09-17: family-law LP, not a foreclosure.
    result = _is_mortgage_preforeclosure(
        "AKEL SUMMAR L", "AKEL NADER A",
        {"case_number": "16-2022-DR-003389-FMXX-MA"},
    )
    assert result is False, f"Expected divorce-case LP to be rejected, got {result}"
    print("PASS: divorce case (DR) rejected")


def test_restoration_lien_rejected():
    # Real case from 2026-09-17: mechanic's lien, not a foreclosure.
    result = _is_mortgage_preforeclosure(
        "ELO RESTORATION LLC", "CHARLES DARLENE",
        {"case_number": "16-2026-CC-018066-AXXX-MA"},
    )
    assert result is False, f"Expected restoration-lien LP to be rejected, got {result}"
    print("PASS: restoration-company lien rejected")


def test_unknown_future_case_type_rejected():
    # The whole point of the allowlist over a denylist: a case type never
    # seen before (probate, small claims, whatever) must be rejected
    # automatically, without needing another manual patch.
    for bad_type in ("PR", "SC", "TR", "CF", "JV", "GC"):
        cn = f"16-2026-{bad_type}-001234-AXXX-MA"
        result = _is_mortgage_preforeclosure("SOME PERSON", "SOME OTHER PERSON", {"case_number": cn})
        assert result is False, f"Expected unknown case type {bad_type} to be rejected, got {result}"
    print("PASS: unknown future case types rejected by allowlist")


def test_real_foreclosures_still_pass():
    # Real records from 2026-09-17 that must NOT regress.
    real_cases = [
        ("GUILD MORTGAGE COMPANY LLC", "MONVIL GREGORY", "16-2026-CA-006479-AXXX-MA"),
        ("BATTEH JERRY E", "CARRIN PAUL JAMES", "16-2026-CA-006483-AXXX-MA"),
        ("THE BANK OF NEW YORK MELLON", "CHEVER ANDREA", "16-2026-CA-006484-AXXX-MA"),
        ("PENNYMAC LOAN SERVICES LLC", "BANKS BRENEE", "16-2026-CA-006486-AXXX-MA"),
    ]
    for plaintiff, defendant, cn in real_cases:
        result = _is_mortgage_preforeclosure(plaintiff, defendant, {"case_number": cn})
        assert result is True, f"Expected real foreclosure to pass: {plaintiff} / {cn}, got {result}"
    print("PASS: real CA-case foreclosures still pass")


def test_cc_case_type_still_allowed():
    # County-civil foreclosures are legitimate too (smaller-dollar cases) --
    # the allowlist must not narrow to CA only.
    result = _is_mortgage_preforeclosure(
        "SOME LENDER LLC", "SOME OWNER", {"case_number": "16-2026-CC-018036-AXXX-MA"},
    )
    assert result is True, f"Expected CC-type foreclosure to pass, got {result}"
    print("PASS: CC case type still allowed")


def test_missing_case_number_does_not_reject():
    # A Details-page fetch timeout/failure means doc_fields has no
    # case_number at all -- that's missing data, not a positive signal of a
    # bad case type, so it must fall through to the existing name-based
    # rules rather than being silently dropped.
    for doc_fields in (None, {}, {"case_number": ""}):
        result = _is_mortgage_preforeclosure("WELLS FARGO BANK NA", "SMITH JOHN", doc_fields)
        assert result is True, f"Expected missing case_number to fall through, got {result} for {doc_fields!r}"
    print("PASS: missing case_number doesn't cause a false rejection")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing LP mortgage-preforeclosure plaintiff/case-type filter")
    print("=" * 60)
    print()

    test_divorce_case_rejected()
    test_restoration_lien_rejected()
    test_unknown_future_case_type_rejected()
    test_real_foreclosures_still_pass()
    test_cc_case_type_still_allowed()
    test_missing_case_number_does_not_reject()

    print()
    print("All tests passed.")
