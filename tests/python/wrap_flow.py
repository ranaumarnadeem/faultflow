"""The IEEE 1500 wrapper flow ff.py wrap starts, for the wrapper mode tests: a small
sky130 block, and the CLI steps that wrap it, scan it, check the scan and run a
wrapper test mode with exported patterns."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from faultflow.config import load_config

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP_PATH = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "chip"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    outputs = ("Q", "X")
    return {
        "hide_name": 0,
        "type": kind,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p in outputs else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _gate(kind: str, a: int, b: int, x: int) -> dict[str, Any]:
    return _cell(f"sky130_fd_sc_hd__{kind}_1", A=a, B=b, X=x)


def ring() -> dict[str, Any]:
    """clk 2, d0 4, d1 5; r0 = d0 ^ r2, r1 = r0 & d1, r2 = r1 | d0; y = r2 ^ r0."""
    cells = {
        "g_x": _gate("xor2", 4, 19, 14),
        "r0": _cell(DFXTP, CLK=2, D=14, Q=15),
        "g_a": _gate("and2", 15, 5, 16),
        "r1": _cell(DFXTP, CLK=2, D=16, Q=17),
        "g_o": _gate("or2", 17, 4, 18),
        "r2": _cell(DFXTP, CLK=2, D=18, Q=19),
        "g_y": _gate("xor2", 19, 15, 20),
    }
    ports = {
        **{n: {"direction": "input", "bits": [b]} for n, b in (("clk", 2), ("d0", 4))},
        "d1": {"direction": "input", "bits": [5]},
        "y": {"direction": "output", "bits": [20]},
    }
    module = {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
    module["netnames"] = {}
    return {"modules": {TOP: module}}


# An accumulator with an asynchronous reset, which a wrapper leaves unwrapped: a
# block Yosys synthesizes, for the flows on RTL.
ACCUMULATOR = """\
module acc (input clk, input rst_n, input en, input [1:0] a, input [1:0] b,
            output [1:0] sum, output carry);
  reg [1:0] r;
  reg c;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      r <= 2'b00;
      c <= 1'b0;
    end else if (en)
      {c, r} <= r + a + b;
  assign sum = r ^ b;
  assign carry = c & en;
endmodule
"""


def run_mode(
    work: Path,
    command: str,
    sections: str = "",
    netlist: Path | None = None,
    top: str = TOP,
    wrap: str = "",
) -> tuple[Any, Path]:
    """init, scan (the wrapper first), scan-check and `command` (intest, extest)
    with exported patterns on `netlist` (the ring by default; RTL Yosys
    synthesizes), run in `work`: the config and the patterns' path. `wrap`: more
    [wrap] keys."""
    from faultflow.cli import main

    if netlist is None:
        netlist = work / "chip.json"
        netlist.write_text(json.dumps(ring()), encoding="utf-8")
    ofs = work / "chip.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {netlist}\ncell_lib = {CELL_MAP_PATH}\n"
        f"liberty = {LIBERTY}\n\n[wrap]\nenabled = true\n{wrap}\n"
        f"[scan]\nchains = 1\n\n{sections}",
        encoding="utf-8",
    )
    patterns = work / f"{command}_patterns.json"
    for step in (["init"], ["scan"], ["scan-check"]):
        assert main([*step, "--top", top, "-c", str(ofs)]) == 0, step
    run = [command, "--export-patterns", str(patterns), "--top", top]
    assert main([*run, "-c", str(ofs)]) == 0
    return load_config(ofs, top), patterns


def faults(cfg: Any, campaign_type: str = "scan") -> list[sqlite3.Row]:
    """The latest `campaign_type` campaign's faults."""
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT fault_site_key, lower(fault_type) AS type, status, exclusion, "
            "blackbox_unresolved, collapsed_into FROM faults WHERE campaign_id = "
            "(SELECT MAX(id) FROM campaigns WHERE campaign_type = ?)",
            (campaign_type,),
        ).fetchall()
    finally:
        conn.close()
