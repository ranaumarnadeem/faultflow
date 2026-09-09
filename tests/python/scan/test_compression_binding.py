from __future__ import annotations

import pytest


@pytest.mark.unit
def test_solve_xor_broadcast_satisfiable(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    # 2 channels -> 3 internal bits: bit0=ch0, bit1=ch1, bit2=ch0^ch1
    fanout = [[0], [1], [0, 1]]
    care_bits = [(0, True), (1, False)]
    result = core.solve_xor_broadcast(2, fanout, care_bits)
    assert result["ok"] is True
    assert result["channels"] == [True, False]


@pytest.mark.unit
def test_solve_xor_broadcast_unsatisfiable(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    fanout = [[0], [1], [0, 1]]
    # bit2 = ch0^ch1 must equal ch0 XOR ch1; force a contradiction: bit0=1,
    # bit1=1, bit2=1 (should be 0 = 1^1) is unsatisfiable.
    care_bits = [(0, True), (1, True), (2, True)]
    result = core.solve_xor_broadcast(2, fanout, care_bits)
    assert result["ok"] is False


@pytest.mark.unit
def test_solve_xor_broadcast_empty_care_bits_trivially_satisfiable(
    require_cpp_core: None,
) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    fanout = [[0], [1]]
    result = core.solve_xor_broadcast(2, fanout, [])
    assert result["ok"] is True
    assert result["channels"] == [False, False]
