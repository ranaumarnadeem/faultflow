"""Tests for the compute_fault_cone_sizes binding used by cone-size ordering.

The binding returns, per fault, the number of nets in the fault's structural
cone (its transitive fan-out plus the fan-in feeding that fan-out). This
approximates the per-fault SAT problem size, so ordering ascending by it runs the
cheapest SAT calls first. The size is purely structural and independent of the
test mode / observation set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
C17 = ROOT / "tests/benchmarks/iscas85/synth_sky130/c17.json"
SKY130 = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _core():
    from faultflow.runner import runner as runner_mod

    core = runner_mod._load_core()
    if core is None:
        pytest.skip("C++ extension _faultflow_core is required")
    return core


def _all_net_indices(core, netlist: Path, cell_map: Path) -> list[int]:
    sites = core.list_site_keys(str(netlist), str(cell_map), "fail")
    return [int(s["compiled_net_index"]) for s in sites]


@pytest.mark.unit
def test_cone_sizes_are_positive_and_vary() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    idx = _all_net_indices(core, C17, SKY130)
    fault_ids = list(range(1000, 1000 + len(idx)))

    sizes = core.compute_fault_cone_sizes(
        str(C17), str(SKY130), fault_ids, idx, "fail", []
    )

    assert len(sizes) == len(fault_ids)
    values = [int(v) for v in sizes.values()]
    assert all(v >= 1 for v in values), "every real net is in its own cone"
    # A circuit with logic depth must produce a spread of cone sizes, otherwise
    # the ordering would be meaningless.
    assert min(values) < max(values)


@pytest.mark.unit
def test_out_of_range_net_index_is_zero() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    # A negative or too-large index must not crash; it sorts last (size 0).
    sizes = core.compute_fault_cone_sizes(
        str(C17), str(SKY130), [1, 2], [-1, 1_000_000], "fail", []
    )
    assert sizes[1] == 0
    assert sizes[2] == 0


@pytest.mark.unit
def test_length_mismatch_raises() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    with pytest.raises(Exception):
        core.compute_fault_cone_sizes(str(C17), str(SKY130), [1, 2], [0], "fail", [])


@pytest.mark.unit
def test_structural_reasons_c17_all_controllable_observable() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    idx = _all_net_indices(core, C17, SKY130)
    fault_ids = list(range(2000, 2000 + len(idx)))

    reasons = core.compute_fault_structural_reasons(
        str(C17), str(SKY130), fault_ids, idx, "fail", []
    )

    assert len(reasons) == len(fault_ids)
    # c17 has no constants: every real net is reachable from a PI and reaches an
    # observable, so nothing is structurally uncontrollable/unobservable.
    for v in reasons.values():
        assert v["reaches_observable"] is True
        assert v["reachable_from_pi"] is True


@pytest.mark.unit
def test_structural_reasons_out_of_range_index_defaults_true() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    # Unknown net index must not crash and must assert no structural reason.
    reasons = core.compute_fault_structural_reasons(
        str(C17), str(SKY130), [1, 2], [-1, 1_000_000], "fail", []
    )
    assert reasons[1]["reachable_from_pi"] is True
    assert reasons[2]["reaches_observable"] is True


@pytest.mark.unit
def test_structural_reasons_length_mismatch_raises() -> None:
    if not C17.exists():
        pytest.skip("c17 netlist missing")
    core = _core()
    with pytest.raises(Exception):
        core.compute_fault_structural_reasons(
            str(C17), str(SKY130), [1, 2], [0], "fail", []
        )
