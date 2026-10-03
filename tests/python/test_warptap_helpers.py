"""FAULTFLOW_REQUIRE_WARPTAP turns the warptap skips into failures."""

from __future__ import annotations

import pytest

from warptap_helpers import REQUIRE_ENV, skip_unless_warptap

MISSING = "warptap_helpers_no_such_module"


@pytest.mark.unit
def test_a_missing_module_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match=MISSING):
        skip_unless_warptap(MISSING)


@pytest.mark.unit
def test_a_missing_module_fails_when_the_run_requires_warptap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_ENV):
        skip_unless_warptap(MISSING)


@pytest.mark.unit
def test_a_missing_parent_package_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        skip_unless_warptap(f"{MISSING}.tap_integrity")


@pytest.mark.unit
def test_an_importable_module_neither_skips_nor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRE_ENV, "1")
    skip_unless_warptap("json")
