"""Grading with a TCK program (faultflow.jtag.grade) on a two-flop TDI -> TDO shift
register behind tck/trst_n: the cycle expansion, the golden gate, and fault credit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.jtag.grade import (
    FaultRow,
    JtagGradeError,
    driven_columns,
    golden_gate,
    grade_faults,
    sim_cycles,
)
from faultflow.jtag.program import TapParams, TckProgram
from faultflow.jtag.verify import _rows, _stubs, _testbench
from faultflow.jtag.xcheck import ChipReset, JtagPorts, JtagSetup, analyze
from faultflow.runner.runner import _load_core

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "jtag_shift"
TDI_BITS = [1, 0, 1, 1, 0, 0, 1, 0]


def _cell(cell_type: str, **conns: int) -> dict[str, Any]:
    return {
        "type": f"sky130_fd_sc_hd__{cell_type}",
        "port_directions": {
            p: ("output" if p in ("Q", "X", "Y") else "input") for p in conns
        },
        "connections": {p: [n] for p, n in conns.items()},
    }


def _netlist(tmp_path: Path) -> Path:
    """tdi -> t0 -> t1 -> tdo (buf), both dfrtp on tck, reset by trst_n."""
    ports = {
        name: {"direction": "input", "bits": [net]}
        for name, net in {"tck": 2, "tms": 3, "tdi": 4, "trst_n": 5}.items()
    }
    ports["tdo"] = {"direction": "output", "bits": [10]}
    design = {
        "modules": {
            TOP: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": ports,
                "cells": {
                    "t0": _cell("dfrtp_1", CLK=2, D=4, RESET_B=5, Q=8),
                    "t1": _cell("dfrtp_1", CLK=2, D=8, RESET_B=5, Q=9),
                    "b0": _cell("buf_1", A=9, X=10),
                },
                "netnames": {n: {"bits": p["bits"]} for n, p in ports.items()},
            }
        }
    }
    path = tmp_path / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


def _program(*, trst: bool = True, expected: list[int] | None = None) -> TckProgram:
    """Three TRST periods (reset), then TDI_BITS shifted, every one sampled: TDO is TDI
    two periods late, 0 until the first bit arrives."""
    lead = 3 if trst else 0
    tdi = [0] * lead + TDI_BITS
    tdo = expected if expected is not None else [0] * lead + [0, 0] + TDI_BITS[:-2]
    count = len(tdi)
    shift = [0] * lead + [1] * len(TDI_BITS)

    def column(bits: list[int]) -> str:
        return "".join(str(b) for b in bits)

    tests = ((("reset", 0, lead),) if trst else ()) + (("shift", lead, count),)
    return TckProgram(
        tap=TapParams(ir_width=4, has_idcode=True, idcode_value=0),
        tests=tests,
        tms="0" * count,
        tdi=column(tdi),
        trst_n=(
            column([0] * (lead - 1) + [1] * (count - lead + 1)) if trst else "1" * count
        ),
        shift=column(shift),
        tdo=column(tdo),
        care=column(shift),
    )


def _setup(netlist: Path):
    return analyze(
        _load_core(),
        netlist,
        CELL_MAP,
        TOP,
        ports=JtagPorts(),
        holds={},
        unsupported="fail",
    )


def _faults(netlist: Path) -> list[FaultRow]:
    core = _load_core()
    assert core is not None
    rows = core.list_site_keys(str(netlist), str(CELL_MAP), "fail")
    faults = []
    for index, row in enumerate(rows):
        for kind in ("sa0", "sa1"):
            faults.append(
                FaultRow(
                    id=2 * index + (kind == "sa1"),
                    site_key=str(row["site_key"]),
                    fault_type=kind,
                    compiled_net_index=int(row["compiled_net_index"]),
                )
            )
    return faults


def test_each_tck_period_is_tck_low_then_high(tmp_path, require_cpp_core):
    setup = _setup(_netlist(tmp_path))
    program = _program()
    cycles, sample = sim_cycles(program, setup)
    assert setup.input_order == ("tck", "tms", "tdi", "trst_n")
    assert len(cycles) == 2 * len(program)
    assert [row[0] for row in cycles[:4]] == [False, True, False, True]
    assert cycles[7] == [True] + cycles[6][1:]  # the rising edge changes only tck
    assert sample == [bit for s in program.shift for bit in (s == "1", False)]


def test_golden_gate_passes_the_right_expectation(tmp_path, require_cpp_core):
    netlist = _netlist(tmp_path)
    tdo = golden_gate(
        _load_core(), netlist, CELL_MAP, _program(), _setup(netlist), unsupported="fail"
    )
    assert tdo == [0, 0] + TDI_BITS[:-2]


def test_golden_gate_names_the_first_disagreement(tmp_path, require_cpp_core):
    netlist = _netlist(tmp_path)
    wrong = [0, 0, 0] + [0, 0] + TDI_BITS[:-2]
    wrong[6] ^= 1  # period 6: the shift test's fourth sample
    with pytest.raises(JtagGradeError, match="in test shift at TCK period 6"):
        golden_gate(
            _load_core(),
            netlist,
            CELL_MAP,
            _program(expected=wrong),
            _setup(netlist),
            unsupported="fail",
        )


def test_golden_gate_refuses_tdo_that_depends_on_the_initial_state(
    tmp_path, require_cpp_core
):
    """Without the TRST lead-in the flops start unknown: the first samples show it."""
    netlist = _netlist(tmp_path)
    with pytest.raises(JtagGradeError, match="depends on the flops' initial state"):
        golden_gate(
            _load_core(),
            netlist,
            CELL_MAP,
            _program(trst=False),
            _setup(netlist),
            unsupported="fail",
        )


def test_faults_are_credited_to_their_first_detecting_test(tmp_path, require_cpp_core):
    netlist = _netlist(tmp_path)
    setup = _setup(netlist)
    faults = _faults(netlist)
    result = grade_faults(
        _load_core(), netlist, CELL_MAP, _program(), setup, faults, unsupported="fail"
    )
    by_key = {(f.site_key, f.fault_type): f.id for f in faults}
    # tdi stuck at 0: TDO's first 1 is TDI's first bit, two periods late (period 5).
    assert result.detected[by_key[("net:4:stem", "sa0")]] == ("shift", 5)
    # trst_n stuck at 1 would leave both flops unreset: ungraded, never credited.
    stuck_inactive = by_key[("net:5:stem", "sa1")]
    assert stuck_inactive in result.reset_path
    assert stuck_inactive not in result.detected
    # trst_n stuck at 0 holds them in reset: TDO stays 0, detected at TDI's first 1.
    assert result.detected[by_key[("net:5:stem", "sa0")]] == ("shift", 5)
    assert result.graded == len(faults) - len(result.reset_path)


def test_bit_parallel_and_reference_grading_agree(tmp_path, require_cpp_core):
    netlist = _netlist(tmp_path)
    setup = _setup(netlist)
    faults = _faults(netlist)
    args = (_load_core(), netlist, CELL_MAP, _program(), setup, faults)
    fast = grade_faults(*args, unsupported="fail", sim_threads=2)
    slow = grade_faults(*args, unsupported="fail", reference=True)
    assert fast == slow


def _pulsing() -> JtagSetup:
    """A chip with rst_n pulsed and clk held."""
    return JtagSetup(
        ports=JtagPorts(),
        input_order=("tck", "tms", "tdi", "trst_n", "rst_n", "clk"),
        holds={"clk": 0},
        tap_flops=(),
        frozen_known=(),
        x_sources=(),
        reset_path_faults=frozenset(),
        pulse=ChipReset("rst_n", 0),
    )


def test_a_pulsed_chip_reset_is_active_through_the_trst_lead_in() -> None:
    """_program starts with two periods of TRST_N low: rst_n is 0 in both, 1 after."""
    program, setup = _program(), _pulsing()
    cycles, _ = sim_cycles(program, setup)
    after = len(program) - 2
    assert [row[4] for row in cycles] == [False] * 4 + [True] * (2 * after)
    assert not any(row[5] for row in cycles)  # clk held
    assert driven_columns(program, setup)["rst_n"] == "00" + "1" * after
    with pytest.raises(JtagGradeError, match="doesn't start with TRST_N low"):
        sim_cycles(_program(trst=False), setup)


def test_the_gate_level_replay_drives_the_pulse_with_the_tap_inputs() -> None:
    program, setup = _program(), _pulsing()
    module = {
        "ports": {
            **{
                name: {"direction": "input", "bits": [2 + i]}
                for i, name in enumerate(setup.input_order)
            },
            "tdo": {"direction": "output", "bits": [20]},
        }
    }
    bench = _testbench(module, "chip", program, setup)
    assert "reg [4:0] jtag_rows" in bench
    assert "{tms, tdi, trst_n, rst_n} = jtag_rows[i][4:1];" in bench
    assert "clk = 1'b0;" in bench and "rst_n = 1'b" not in bench
    # tms, tdi, trst_n, rst_n, then whether the period shifts.
    assert _rows(program, setup)[:3] == ["00000", "00000", "00110"]


def test_a_blackbox_stub_takes_every_instances_pins_and_parameters() -> None:
    """One memory type, instantiated bare and with overrides (a collar's), one pin
    left unconnected on one of them: the stub has all of them."""

    def memory(params: dict[str, str], pins: dict[str, list[int]]) -> dict[str, Any]:
        directions = {pin: "output" if pin == "dout" else "input" for pin in pins}
        return {
            "type": "sram",
            "parameters": params,
            "port_directions": directions,
            "connections": pins,
        }

    module = {
        "cells": {
            "u_bare": memory({}, {"clk": [2], "addr": [3, 4]}),
            "u_collar.u_sram": memory(
                {"ADDR_WIDTH": "2"}, {"clk": [2], "addr": [5, 6], "dout": [7]}
            ),
        }
    }
    stub = _stubs(module, {"u_bare", "u_collar.u_sram"})
    assert stub.count("module sram") == 1
    assert "#(parameter ADDR_WIDTH = 0)" in stub
    assert "input wire [1:0] addr" in stub and "output wire dout" in stub
