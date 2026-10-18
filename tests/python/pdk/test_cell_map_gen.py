"""Cell maps generated from Liberty (faultflow.pdk.cell_map_gen): each combinational
cell's function matched to one of the engine's gate types by exhaustive truth table (the
core's own gate evaluation is the oracle), flops read from their `ff` and `test_cell`
groups, and every other cell -- latches, tristates, clock gates, physical cells,
functions no gate type has -- listed explicitly, never left out and never guessed."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.pdk.boolexpr import parse_expr
from faultflow.pdk.cell_map_gen import generate_cell_map
from faultflow.pdk.liberty import parse_liberty

ROOT = Path(__file__).resolve().parents[3]
HD_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
HD_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _pin(name: str, direction: str = "input", **attrs: str) -> str:
    body = "".join(f' {k} : "{v}";' for k, v in attrs.items())
    return f'pin ("{name}") {{ direction : "{direction}";{body} }}\n'


def _cell(name: str, body: str) -> str:
    power = 'pg_pin ("VPWR") { pg_type : "primary_power"; }'
    return f'cell ("{name}") {{\n {power}\n{body}}}\n'


def _lib(*cells: str) -> str:
    return 'library ("demo") {\n' + "".join(cells) + "}\n"


def _comb(name: str, inputs: list[str], outputs: dict[str, str]) -> str:
    body = "".join(_pin(p) for p in inputs)
    body += "".join(_pin(o, "output", function=f) for o, f in outputs.items())
    return _cell(name, body)


def _generate(text: str, **kwargs: Any):  # type: ignore[no-untyped-def]
    from faultflow.runner.runner import _load_core

    return generate_cell_map(parse_liberty(text), _load_core(), **kwargs)


def _equivalent(
    core: Any, entry: dict[str, Any], function: str, pins: list[str]
) -> bool:
    expr = parse_expr(function)
    for values in itertools.product([False, True], repeat=len(pins)):
        env = dict(zip(pins, values))
        got = core.eval_gate(entry["gate_type"], [env[p] for p in entry["inputs"]])
        if bool(got) != expr.eval(env):
            return False
    return True


@pytest.fixture
def core(require_cpp_core: None) -> Any:
    from faultflow.runner.runner import _load_core

    return _load_core()


def test_a_combinational_cell_gets_the_gate_type_its_function_is(core: Any) -> None:
    text = _lib(
        _comb("demo__nand2_1", ["A", "B"], {"Y": "!(A&B)"}),
        _comb("demo__o21ai_2", ["A1", "A2", "B1"], {"Y": "(!A1&!A2) | (!B1)"}),
        _comb("demo__mux2i_1", ["A0", "A1", "S"], {"Y": "(!A0&!S) | (!A1&S)"}),
        _comb("demo__inv_1", ["A"], {"Y": "!A"}),
    )
    result = _generate(text)
    nand = result.cell_map["demo__nand2_*"]
    assert (nand["node_type"], nand["gate_type"], nand["outputs"]) == (
        "GATE",
        "NAND2",
        {"Y": "Y"},
    )
    for key, fn, pins in [
        ("demo__o21ai_*", "(!A1&!A2) | (!B1)", ["A1", "A2", "B1"]),
        ("demo__mux2i_*", "(!A0&!S) | (!A1&S)", ["A0", "A1", "S"]),
        ("demo__inv_*", "!A", ["A"]),
    ]:
        entry = result.cell_map[key]
        assert sorted(entry["inputs"]) == sorted(pins)
        assert _equivalent(core, entry, fn, pins), key


def test_drive_strengths_share_a_pattern_unless_another_family_would_match_it(
    core: Any,
) -> None:
    text = _lib(
        _comb("demo__buf_1", ["A"], {"X": "A"}),
        _comb("demo__buf_4", ["A"], {"X": "A"}),
        _comb("demo__iso_1", ["A"], {"X": "A"}),
        _comb("demo__iso_tap_1", ["A", "B"], {"X": "A&B"}),
    )
    cm = _generate(text).cell_map
    assert "demo__buf_*" in cm
    # demo__iso_* would also match demo__iso_tap_1: that family gets its own names;
    # demo__iso_tap_* matches only its own and keeps the pattern.
    assert "demo__iso_*" not in cm
    assert cm["demo__iso_1"]["gate_type"] == "BUF"
    assert cm["demo__iso_tap_*"]["gate_type"] == "AND2"


def test_a_single_output_xor_is_never_an_adders_sum(core: Any) -> None:
    """An adder's sum is an XOR too, but the compiler lowers an adder by its output
    pins' names: a one-output cell must get XOR2 / XOR3."""
    text = _lib(
        _comb("demo__xor2_lp", ["A", "B"], {"X": "(A&!B) | (!A&B)"}),
        _comb("demo__xor3_lp", ["A", "B", "C"], {"X": "(A^B)^C"}),
        _comb("demo__xor2_1", ["A", "B"], {"X": "A^B"}),
    )
    cm = _generate(text).cell_map
    assert cm["demo__xor2_*"]["gate_type"] == "XOR2"
    assert cm["demo__xor3_*"]["gate_type"] == "XOR3"


def test_low_power_drive_suffixes_are_drive_strengths(core: Any) -> None:
    """sky130_fd_sc_lp names drive variants _lp, _lp2 and _m beside _1, _2, ..."""
    text = _lib(
        _comb("demo__nand2_1", ["A", "B"], {"Y": "!(A&B)"}),
        _comb("demo__nand2_lp", ["A", "B"], {"Y": "!(A&B)"}),
        _comb("demo__nand2_lp2", ["A", "B"], {"Y": "!(A&B)"}),
        _comb("demo__nand2_m", ["A", "B"], {"Y": "!(A&B)"}),
    )
    cm = _generate(text).cell_map
    assert [k for k in cm if "nand2" in k] == ["demo__nand2_*"]


def test_tie_cells_are_constants(core: Any) -> None:
    """A tie cell drives constants: one output (a tiehi) is a CONST entry; Sky130's
    conb, HI and LO, one CONST entry whose ``constants`` say which is which."""
    text = _lib(
        _comb("demo__conb_1", [], {"HI": "1", "LO": "0"}),
        _comb("demo__tiehi_1", [], {"Y": "1"}),
    )
    cm = _generate(text).cell_map
    assert cm["demo__conb_*"] == {
        "node_type": "CONST",
        "inputs": [],
        "outputs": {"HI": "HI", "LO": "LO"},
        "constants": {"HI": 1, "LO": 0},
        "variants": ["demo__conb_1"],
    }
    assert cm["demo__tiehi_*"]["gate_type"] == "CONST1"
    assert cm["demo__tiehi_*"]["outputs"] == {"Y": "Y"}


def test_a_full_adder_is_one_entry_with_both_outputs(core: Any) -> None:
    text = _lib(
        _comb(
            "demo__fa_1",
            ["A", "B", "CIN"],
            {"COUT": "(A&B) | (A&CIN) | (B&CIN)", "SUM": "(A^B)^CIN"},
        ),
        _comb("demo__ha_1", ["A", "B"], {"COUT": "A&B", "SUM": "A^B"}),
    )
    cm = _generate(text).cell_map
    assert cm["demo__fa_*"]["gate_type"] == "ADDF"
    assert cm["demo__fa_*"]["outputs"] == {"S": "SUM", "CO": "COUT"}
    assert sorted(cm["demo__fa_*"]["inputs"]) == ["A", "B", "CIN"]
    assert cm["demo__ha_*"]["gate_type"] == "ADDH"
    assert cm["demo__ha_*"]["outputs"] == {"S": "SUM", "CO": "COUT"}


def _ff_cell(name: str, ff: dict[str, str], pins: list[str], extra: str = "") -> str:
    attrs = "".join(f' {k} : "{v}";' for k, v in ff.items())
    body = f' ff ("IQ","IQ_N") {{{attrs} }}\n'
    body += "".join(_pin(p) for p in pins)
    body += _pin("Q", "output", function="IQ")
    return _cell(name, body + extra)


def test_a_scan_flop_with_reset(core: Any) -> None:
    test_cell = (
        ' test_cell () { ff ("IQ","IQ_N") { clocked_on : "CLK"; next_state : "D";'
        ' clear : "!RESET_B"; }\n'
        + _pin("SCD", signal_type="test_scan_in")
        + _pin("SCE", signal_type="test_scan_enable")
        + _pin("Q", "output", function="IQ", signal_type="test_scan_out")
        + " }\n"
    )
    text = _lib(
        _ff_cell(
            "demo__sdfrtp_1",
            {
                "clocked_on": "CLK",
                "next_state": "(D&!SCE) | (SCD&SCE)",
                "clear": "!RESET_B",
            },
            ["CLK", "D", "RESET_B", "SCD", "SCE"],
            test_cell,
        )
    )
    entry = _generate(text).cell_map["demo__sdfrtp_*"]
    assert entry["node_type"] == "FF"
    assert entry["outputs"] == {"Q": "Q"}
    assert entry["ff"] == {
        "clock": "CLK",
        "data": "D",
        "output": "Q",
        "trigger": "POSEDGE",
        "clear": {"pin": "RESET_B", "level": "LOW", "value": 0},
        "scan": {"in": "SCD", "enable": "SCE", "enable_polarity": "HIGH"},
    }
    assert set(entry["inputs"]) == {"CLK", "D", "RESET_B", "SCD", "SCE"}


def test_an_enable_flop_and_a_negative_edge_one(core: Any) -> None:
    text = _lib(
        _ff_cell(
            "demo__edfxtp_1",
            {"clocked_on": "CLK", "next_state": "(D&DE) | (IQ&!DE)"},
            ["CLK", "D", "DE"],
        ),
        _ff_cell(
            "demo__dfxtn_1",
            {"clocked_on": "!CLK_N", "next_state": "D"},
            ["CLK_N", "D"],
        ),
    )
    cm = _generate(text).cell_map
    assert cm["demo__edfxtp_*"]["ff"]["enable"] == {"pin": "DE", "level": "HIGH"}
    assert cm["demo__edfxtp_*"]["ff"]["data"] == "D"
    neg = cm["demo__dfxtn_*"]["ff"]
    assert (neg["clock"], neg["trigger"]) == ("CLK_N", "NEGEDGE")


def test_clear_and_preset_together_take_the_librarys_conflict_value(core: Any) -> None:
    qn = _pin("Q_N", "output", function="IQ_N")
    text = _lib(
        _ff_cell(
            "demo__dfbbp_1",
            {
                "clocked_on": "CLK",
                "next_state": "D",
                "clear": "!RESET_B",
                "preset": "!SET_B",
                "clear_preset_var1": "H",
                "clear_preset_var2": "L",
            },
            ["CLK", "D", "RESET_B", "SET_B"],
            qn,
        )
    )
    entry = _generate(text).cell_map["demo__dfbbp_*"]
    assert entry["ff"]["clear"] == {"pin": "RESET_B", "level": "LOW", "value": 0}
    assert entry["ff"]["preset"] == {"pin": "SET_B", "level": "LOW", "value": 1}
    assert entry["ff"]["clear_preset_conflict_value"] == 1
    assert entry["outputs"] == {"Q": "Q", "QN": "Q_N"}


def test_cells_the_engine_does_not_model_are_listed_explicitly(core: Any) -> None:
    latch = _cell(
        "demo__dlxtp_1",
        ' latch ("IQ","IQ_N") { data_in : "D"; enable : "GATE"; }\n'
        + _pin("D")
        + _pin("GATE")
        + _pin("Q", "output", function="IQ"),
    )
    icg = _cell(
        "demo__dlclkp_1",
        ' clock_gating_integrated_cell : "latch_posedge";\n'
        + _pin("CLK")
        + _pin("GATE")
        + _pin("GCLK", "output", state_function="(CLK*M0)"),
    )
    tbuf = _cell(
        "demo__ebufn_1",
        _pin("A")
        + _pin("TE_B")
        + _pin("Z", "output", function="(A)", three_state="(TE_B)"),
    )
    # A tristate bus mux whose output has no function, only a three_state (hdll's
    # muxb4to1): a tristate, not a physical cell.
    muxb = _cell(
        "demo__muxb4to1_1",
        _pin("D") + _pin("S") + _pin("Z", "output", three_state="(!S)"),
    )
    probe = _comb("demo__probe_p_8", ["A"], {"X": "A"})
    decap = _cell("demo__decap_4", "")
    odd = _comb("demo__odd_1", ["A", "B", "C", "D"], {"Y": "!(A&B&C) ^ D"})
    result = _generate(_lib(latch, icg, tbuf, muxb, probe, decap, odd))
    cm = result.cell_map
    assert cm["demo__dlxtp_*"]["unsupported"] is True
    assert "latch" in cm["demo__dlxtp_*"]["reason"]
    assert cm["demo__dlclkp_*"]["unsupported"] is True
    assert "clock-gating" in cm["demo__dlclkp_*"]["reason"]
    assert cm["demo__ebufn_*"] == {"node_type": "TBUF", "reason": "tristate output"}
    assert cm["demo__muxb4to1_*"] == {"node_type": "TBUF", "reason": "tristate output"}
    assert cm["demo__decap_*"]["unsupported"] is True
    assert "physical" in cm["demo__decap_*"]["reason"]
    assert cm["demo__odd_*"]["unsupported"] is True
    assert "no gate type" in cm["demo__odd_*"]["reason"]
    # The report names every cell that isn't modelled, with why.
    listed = {r.cell: r.kind for r in result.report}
    assert listed["demo__odd_1"] == "no_gate_type"
    assert listed["demo__dlxtp_1"] == "latch"
    assert listed["demo__decap_4"] == "physical"
    # A probe buffer is physical by policy, whatever its function.
    assert listed["demo__probe_p_8"] == "physical"
    assert cm["demo__probe_p_*"]["unsupported"] is True


def test_a_clock_gate_maps_from_its_liberty_pins(core: Any) -> None:
    """An integrated clock gate's kind and its clock_gate_*_pin attributes: Sky130's
    sdlclkp (latch_posedge_precontrol, a test enable) and GF180MCU's icgtn
    (latch_negedge_precontrol), its pins named otherwise."""
    pos = _cell(
        "demo__sdlclkp_1",
        ' clock_gating_integrated_cell : "latch_posedge_precontrol";\n'
        + _pin("CLK", clock_gate_clock_pin="true")
        + _pin("GATE", clock_gate_enable_pin="true")
        + _pin("SCE", clock_gate_test_pin="true")
        + _pin("GCLK", "output", clock_gate_out_pin="true"),
    )
    neg = _cell(
        "demo__icgtn_1",
        ' clock_gating_integrated_cell : "latch_negedge_precontrol";\n'
        + _pin("CLKN", clock_gate_clock_pin="true")
        + _pin("E", clock_gate_enable_pin="true")
        + _pin("TE", clock_gate_test_pin="true")
        + _pin("Q", "output", clock_gate_out_pin="true"),
    )
    # A _precontrol gate without its test pin is listed, never guessed.
    broken = _cell(
        "demo__icgx_1",
        ' clock_gating_integrated_cell : "latch_posedge_precontrol";\n'
        + _pin("CLK", clock_gate_clock_pin="true")
        + _pin("GATE", clock_gate_enable_pin="true")
        + _pin("GCLK", "output", clock_gate_out_pin="true"),
    )
    cm = _generate(_lib(pos, neg, broken)).cell_map
    assert cm["demo__sdlclkp_*"] == {
        "node_type": "ICG",
        "inputs": ["CLK", "GATE", "SCE"],
        "outputs": {"GCLK": "GCLK"},
        "icg": {
            "clock": "CLK",
            "enable": "GATE",
            "test": "SCE",
            "output": "GCLK",
            "edge": "POSEDGE",
        },
        "variants": ["demo__sdlclkp_1"],
    }
    assert cm["demo__icgtn_*"]["icg"] == {
        "clock": "CLKN",
        "enable": "E",
        "test": "TE",
        "output": "Q",
        "edge": "NEGEDGE",
    }
    assert cm["demo__icgx_*"]["unsupported"] is True
    assert "test pin" in cm["demo__icgx_*"]["reason"]


def test_internal_cells_are_carried_over(core: Any) -> None:
    base = {"$scanff_faultflow": {"node_type": "FF", "ff": {"clock": "CLK"}}}
    cm = _generate(
        _lib(_comb("demo__inv_1", ["A"], {"Y": "!A"})), internal_cells=base
    ).cell_map
    assert cm["$scanff_faultflow"] == base["$scanff_faultflow"]
    assert list(cm)[0] == "$scanff_faultflow"


def _expand(entry: dict[str, Any], core: Any, pins: list[str]) -> dict[int, bool]:
    out = {}
    for i, values in enumerate(itertools.product([False, True], repeat=len(pins))):
        env = dict(zip(pins, values))
        out[i] = bool(
            core.eval_gate(entry["gate_type"], [env[p] for p in entry["inputs"]])
        )
    return out


@pytest.mark.integration
def test_the_hd_map_regenerated_agrees_with_the_shipped_one(core: Any) -> None:
    """Every cell the shipped HD map models, the generated one models the same way: the
    same gate function under its pin order, the same flop metadata. Every cell of the
    HD Liberty is either modelled or listed."""
    import fnmatch

    lib = parse_liberty(HD_LIB.read_text(encoding="utf-8"))
    shipped = json.loads(HD_MAP.read_text(encoding="utf-8"))
    result = generate_cell_map(lib, core)
    generated = result.cell_map

    def lookup(cm: dict[str, Any], name: str) -> dict[str, Any] | None:
        for pattern, entry in cm.items():
            if fnmatch.fnmatchcase(name, pattern):
                return entry
        return None

    names = [c.args[0] for c in lib.all("cell")]
    listed = {r.cell for r in result.report}
    for name in names:
        assert lookup(generated, name) is not None or name in listed, name
    for name in names:
        old = lookup(shipped, name)
        if (
            old is None
            or old.get("unsupported")
            or old.get("node_type")
            not in (
                "GATE",
                "FF",
                "ICG",
            )
        ):
            continue
        new = lookup(generated, name)
        assert new is not None and new.get("node_type") == old["node_type"], name
        if old["node_type"] == "ICG":
            assert new["icg"] == old["icg"], name
            assert new["inputs"] == old["inputs"] and new["outputs"] == old["outputs"]
        elif old["node_type"] == "GATE":
            pins = sorted(set(old["inputs"]))
            assert sorted(new["inputs"]) == pins, name
            assert new["outputs"] == old["outputs"], name
            assert _expand(new, core, pins) == _expand(old, core, pins), name
        else:
            # The conflict value (clear and preset both active) is the Liberty's
            # clear_preset_var1; the hand-made entries leave it at its default.
            old_ff = {
                k: v for k, v in old["ff"].items() if k != "clear_preset_conflict_value"
            }
            new_ff = {
                k: v for k, v in new["ff"].items() if k != "clear_preset_conflict_value"
            }
            assert new_ff == old_ff, name


@pytest.mark.unit
def test_every_cell_of_the_hd_liberty_has_a_shipped_entry() -> None:
    """Policy 1: a cell the shipped HD map does not model is an explicit entry, never
    absent. Looked up as the C++ core does: the first key in order, a trailing ``*``
    a prefix match."""
    shipped = json.loads(HD_MAP.read_text(encoding="utf-8"))
    names = [
        c.args[0] for c in parse_liberty(HD_LIB.read_text(encoding="utf-8")).all("cell")
    ]

    def lookup(name: str) -> dict[str, Any] | None:
        for key, entry in shipped.items():
            if (key.endswith("*") and name.startswith(key[:-1])) or key == name:
                return dict(entry)
        return None

    absent = [n for n in names if lookup(n) is None]
    assert absent == []
    entries = {n: lookup(n) or {} for n in names}
    modelled = [n for n, entry in entries.items() if not entry.get("unsupported")]
    assert len(modelled) >= 391
