from __future__ import annotations

import pytest

from faultflow.scan.site_resolution import fault_type_to_sa_code


@pytest.mark.unit
def test_fault_type_to_sa_code_accepts_canonical_values() -> None:
    assert fault_type_to_sa_code("sa0") == 0
    assert fault_type_to_sa_code("sa1") == 1


@pytest.mark.unit
def test_fault_type_to_sa_code_rejects_unexpected_values() -> None:
    """Same bug class as the ffa3323 case-mismatch fix (see
    test_fault_type_polarity.py): silently defaulting any non-'sa0' string to
    SA1 means a case mismatch, typo, or unrelated fault-type string reaching
    this stuck-at-only helper by mistake is silently misclassified as SA1
    instead of failing loudly."""
    for bad in ("SA0", "SA1", "sa2", "", "str", "stf"):
        with pytest.raises(ValueError, match="unexpected fault_type"):
            fault_type_to_sa_code(bad)
