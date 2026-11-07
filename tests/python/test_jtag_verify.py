"""ff.py jtag --verify (faultflow.jtag.verify.replay_on_gate_level): the gate-level
replay on the PDK's cell models takes what the golden run takes, on the ideal clock
both grade on -- whatever the clock tree's depth, and on either TCK edge."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.jtag.grade import golden_gate
from faultflow.jtag.program import TapParams, TckProgram
from faultflow.jtag.verify import replay_on_gate_level
from faultflow.jtag.xcheck import JtagPorts, JtagSetup

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
MODELS = ROOT / "cells/sky130/sky130_fd_sc_hd.v"
TOP = "chip"
HD = "sky130_fd_sc_hd"
PORTS = {"tck": 2, "tms": 3, "tdi": 4, "trst_n": 5, "tdo": 6}
Q1, C1, C2 = 7, 8, 9


def _cell(kind: str, **conns: int | str) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": f"{HD}__{kind}",
        "parameters": {},
        "attributes": {},
        "connections": {pin: [net] for pin, net in conns.items()},
    }


def _verilog_cell(name: str, cell: dict[str, Any], nets: dict[int, str]) -> str:
    def wire(bit: int | str) -> str:
        return nets[bit] if isinstance(bit, int) else f"1'b{bit}"

    pins = ", ".join(
        f".{pin}({wire(bits[0])})" for pin, bits in cell["connections"].items()
    )
    return f"  {cell['type']} {name} ({pins});\n"


# A flop on inverted TCK reading TDI directly, its output into a flop on TCK.
FALLING_EDGE = {
    "u_neg": _cell("dfbbn_1", CLK_N=2, D=4, RESET_B=5, SET_B="1", Q=Q1),
    "u_pos": _cell("dfrtp_1", CLK=2, D=Q1, RESET_B=5, Q=6),
}
# A flop on TCK into one two clock buffers deeper.
CLOCK_TREE = {
    "u_src": _cell("dfrtp_1", CLK=2, D=4, RESET_B=5, Q=Q1),
    "u_b1": _cell("clkbuf_1", A=2, X=C1),
    "u_b2": _cell("clkbuf_1", A=C1, X=C2),
    "u_dst": _cell("dfrtp_1", CLK=C2, D=Q1, RESET_B=5, Q=6),
}


@pytest.mark.integration
@pytest.mark.parametrize(
    "cells", [FALLING_EDGE, CLOCK_TREE], ids=["falling_edge", "clock_tree"]
)
def test_the_replay_takes_what_the_golden_run_takes(
    tmp_path: Path, require_cpp_core: None, cells: dict[str, Any]
) -> None:
    from faultflow.runner.runner import _load_core

    for tool in ("iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")
    ports = {
        name: {"direction": "output" if name == "tdo" else "input", "bits": [net]}
        for name, net in PORTS.items()
    }
    module = {
        "attributes": {"top": "1"},
        "ports": ports,
        "cells": cells,
        "netnames": {
            name: {"hide_name": 0, "bits": [net], "attributes": {}}
            for name, net in {**PORTS, "q1": Q1, "c1": C1, "c2": C2}.items()
        },
    }
    netlist = tmp_path / "chip.json"
    netlist.write_text(json.dumps({"modules": {TOP: module}}), encoding="utf-8")
    names = {net: name for name, net in {**PORTS, "q1": Q1, "c1": C1, "c2": C2}.items()}
    verilog = tmp_path / "chip.v"
    verilog.write_text(
        f"module {TOP} (input tck, input tms, input tdi, input trst_n, output tdo);\n"
        "  wire q1, c1, c2;\n"
        + "".join(_verilog_cell(n, c, names) for n, c in cells.items())
        + "endmodule\n",
        encoding="utf-8",
    )
    length = 12
    program = TckProgram(
        tap=TapParams(ir_width=4, has_idcode=False, idcode_value=0),
        tests=(("all", 0, length),),
        tms="0" * length,
        tdi="010110100110",
        trst_n="001111111111",
        shift="000011111111",
        tdo="0" * length,
        care="0" * length,
    )
    setup = JtagSetup(
        ports=JtagPorts(),
        input_order=("tck", "tms", "tdi", "trst_n"),
        holds={},
        tap_flops=tuple(n for n, c in cells.items() if "df" in c["type"]),
        frozen_known=(),
        x_sources=(),
        reset_path_faults=frozenset(),
    )
    core = _load_core()
    golden = golden_gate(core, netlist, CELL_MAP, program, setup, unsupported="fail")
    assert set(golden) == {0, 1}
    replay_on_gate_level(
        verilog, module, TOP, program, setup, golden, models=MODELS, blackbox=set()
    )
