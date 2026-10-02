"""The scan solvers' decompressor constraints through the bindings: each PI named
in seeded_inputs (a scan cell's PPI) is the XOR of its seed bits, and so, for
launch-on-shift, is each chain head named in seeded_heads in the capture frame
(the launch shift's scan-in bit) -- one seed of seed_width bits for the whole
test. A SAT vector is then one the decompressor loads; UNSAT, that no seed tests
the fault. Fixtures: tests/cpp/fixtures/tiny_scan_view_{seeded,loc,los}.json."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from db_v3_helpers import insert_campaign
from faultflow.db import connect, init_schema

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/cpp/fixtures"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

# tiny_scan_view_seeded: Y = AND3(ff0, ff1, ff2). Loaded from a 2-bit seed with
# ff0 = s0, ff1 = s0 ^ s1, ff2 = s1, every load has ff1 = ff0 ^ ff2, so Y
# stuck-at-0 (all three at 1) has no test through the decompressor.
SEEDED = {"__ppi_ff0": [0], "__ppi_ff1": [0, 1], "__ppi_ff2": [1]}


def _view(tmp_path: Path, fixture: str) -> tuple[Any, str, str, dict[str, int]]:
    """The core, the fixture's path, a DB with its faults enumerated, and
    each fault's id by `<site key>/<fault type>`."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    db = tmp_path / "faults.sqlite"
    conn = connect(db)
    init_schema(conn)
    campaign = insert_campaign(conn, top=fixture.removesuffix(".json"))
    conn.commit()
    conn.close()
    view = str(FIXTURES / fixture)
    core.ensure_faults_enumerated(view, str(CELL_MAP), str(db), campaign)
    conn = connect(db)
    rows = conn.execute(
        "SELECT id, fault_site_key, fault_type FROM faults WHERE campaign_id = ?",
        (campaign,),
    ).fetchall()
    conn.close()
    ids = {f"{r['fault_site_key']}/{r['fault_type']}": int(r["id"]) for r in rows}
    return core, view, str(db), ids


def _solve(core: Any, view: str, db: str, fault_id: int, **seed: Any) -> dict[str, Any]:
    return dict(
        core.solve_fault_atpg(view, str(CELL_MAP), db, fault_id, [], -1, 0, **seed)
    )


@pytest.mark.unit
def test_stuck_at_seeded_inputs_decide_the_verdict(
    tmp_path: Path, require_cpp_core: None
) -> None:
    core, view, db, ids = _view(tmp_path, "tiny_scan_view_seeded.json")
    y_sa0 = ids["net:6:stem/sa0"]
    assert _solve(core, view, db, y_sa0)["result"] == "SAT"
    assert (
        _solve(core, view, db, y_sa0, seed_width=2, seeded_inputs=SEEDED)["result"]
        == "UNSAT"
    )
    # Only the named cells are seeded: ff2 left free, Y = 1 is loadable again.
    partial = {"__ppi_ff0": [0], "__ppi_ff1": [0, 1]}
    assert (
        _solve(core, view, db, y_sa0, seed_width=2, seeded_inputs=partial)["result"]
        == "SAT"
    )


@pytest.mark.unit
def test_stuck_at_seeded_vector_is_a_decompressor_load(
    tmp_path: Path, require_cpp_core: None
) -> None:
    core, view, db, ids = _view(tmp_path, "tiny_scan_view_seeded.json")
    for key, fault_id in ids.items():
        solved = _solve(core, view, db, fault_id, seed_width=2, seeded_inputs=SEEDED)
        if solved["result"] != "SAT":
            continue
        vector = solved["vector"]
        assert vector["__ppi_ff1"] == (vector["__ppi_ff0"] != vector["__ppi_ff2"]), key


@pytest.mark.unit
def test_seeded_input_that_is_not_a_pi_is_refused(
    tmp_path: Path, require_cpp_core: None
) -> None:
    core, view, db, ids = _view(tmp_path, "tiny_scan_view_seeded.json")
    with pytest.raises(RuntimeError, match="__ppi_nope"):
        _solve(
            core,
            view,
            db,
            ids["net:6:stem/sa1"],
            seed_width=2,
            seeded_inputs={"__ppi_nope": [0]},
        )


@pytest.mark.unit
def test_loc_seeded_inputs_load_the_launch_frame(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """tiny_scan_view_loc: ff0's next state is A[0] ^ ff0. Its slow-to-fall
    needs ff0 = 1 at launch, which a load row of no seed bits (always 0)
    rules out; slow-to-rise needs 0, which it allows."""
    core, view, db, ids = _view(tmp_path, "tiny_scan_view_loc.json")

    def solve(fault: str, **seed: Any) -> str:
        solved = core.solve_scan_transition_fault_atpg(
            view, str(CELL_MAP), db, ids[fault], [], -1, 0, **seed
        )
        return str(dict(solved)["result"])

    zero = {"seed_width": 1, "seeded_inputs": {"__ppi_ff0": []}}
    assert solve("net:3:stem/sa1") == "SAT"
    assert solve("net:3:stem/sa1", **zero) == "UNSAT"
    assert solve("net:3:stem/sa0", **zero) == "SAT"


@pytest.mark.unit
def test_los_seeded_heads_load_the_launch_shift(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """tiny_scan_view_los: ff2 heads chain 1 alone, so its launch-on-shift
    edge goes from its load to the launch shift's scan-in bit. Slow-to-rise
    needs that bit at 1: a head row of no seed bits rules it out. Slow-to-fall
    needs the load at 1: a load row of no seed bits rules it out."""
    core, view, db, ids = _view(tmp_path, "tiny_scan_view_los.json")
    couples = [("__ppi_ff1", "__ppi_ff0")]
    heads = ["__ppi_ff0", "__ppi_ff2"]

    def solve(fault: str, **seed: Any) -> str:
        solved = core.solve_scan_los_transition_fault_atpg(
            view, str(CELL_MAP), db, ids[fault], couples, heads, [], -1, 0, **seed
        )
        return str(dict(solved)["result"])

    assert solve("net:8:stem/sa0") == "SAT"
    assert solve("net:8:stem/sa0", seed_width=1, seeded_heads={"__ppi_ff2": []}) == (
        "UNSAT"
    )
    assert solve("net:8:stem/sa1") == "SAT"
    assert solve("net:8:stem/sa1", seed_width=1, seeded_inputs={"__ppi_ff2": []}) == (
        "UNSAT"
    )
    # The load row doesn't touch the launch shift's scan-in bit.
    assert solve("net:8:stem/sa0", seed_width=1, seeded_inputs={"__ppi_ff2": []}) == (
        "SAT"
    )
