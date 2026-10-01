"""``ff.py jtag --verify``: replay the TCK program on the techmapped gate-level netlist
in Icarus Verilog -- four-state, with the PDK's cell models -- and require TDO to be the
golden value, never X or Z, at every shift.

An independent check of both the X-isolation proof and the two-valued golden run: every
flop starts at X here, and each blackboxed cell is a port-only stub whose outputs float.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from faultflow.config import FaultflowConfig
from faultflow.jtag.grade import driven_columns
from faultflow.jtag.program import TckProgram
from faultflow.jtag.xcheck import JtagSetup


class JtagVerifyError(RuntimeError):
    """The gate-level replay disagrees with the golden TDO, or can't run."""


def _escaped(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) else f"\\{name} "


def _bit_ref(port: str, index: int, width: int, offset: int) -> str:
    return _escaped(port) if width == 1 else f"{_escaped(port)}[{index + offset}]"


def _stubs(module: Mapping[str, Any], blackbox: set[str]) -> str:
    """A port-only module for each blackboxed cell type: its outputs float (Z). It has
    every pin and parameter any instance of the type connects or overrides."""
    ports: dict[str, dict[str, str]] = {}
    params: dict[str, dict[str, None]] = {}
    for instance, cell in module.get("cells", {}).items():
        if instance not in blackbox:
            continue
        cell_type = str(cell["type"]).lstrip("\\")
        for pin, bits in cell.get("connections", {}).items():
            direction = cell.get("port_directions", {}).get(pin, "input")
            width = f"[{len(bits) - 1}:0] " if len(bits) > 1 else ""
            ports.setdefault(cell_type, {}).setdefault(
                pin, f"    {direction} wire {width}{_escaped(pin)}"
            )
        # The values don't matter to a stub.
        params.setdefault(cell_type, {}).update(
            dict.fromkeys(cell.get("parameters", {}))
        )
    stubs = []
    for cell_type, pins in ports.items():
        declared = ", ".join(f"parameter {_escaped(p)} = 0" for p in params[cell_type])
        header = f"module {_escaped(cell_type)}" + (
            f" #({declared})" if declared else ""
        )
        stubs.append(header + " (\n" + ",\n".join(pins.values()) + "\n);\nendmodule\n")
    return "\n".join(stubs)


def _rows(program: TckProgram, setup: JtagSetup) -> list[str]:
    """One row per TCK period: each driven input's value, then whether it shifts."""
    columns = list(driven_columns(program, setup).values())
    return [
        "".join(column[i] for column in columns) + program.shift[i]
        for i in range(len(program))
    ]


def _testbench(
    module: Mapping[str, Any], top: str, program: TckProgram, setup: JtagSetup
) -> str:
    ports = setup.ports
    columns = driven_columns(program, setup)
    decls: list[str] = []
    conns: list[str] = []
    holds: list[str] = []
    for name, port in module.get("ports", {}).items():
        width = len(port.get("bits", []))
        offset = int(port.get("offset", 0) or 0)
        kind = "reg" if port.get("direction") == "input" else "wire"
        rng = f"[{width - 1 + offset}:{offset}] " if width > 1 else ""
        decls.append(f"    {kind} {rng}{_escaped(name)};")
        conns.append(f"        .{_escaped(name)}({_escaped(name)})")
        if port.get("direction") != "input":
            continue
        for index in range(width):
            bit = name if width == 1 else f"{name}[{index}]"
            if bit in setup.holds:
                holds.append(
                    f"        {_bit_ref(name, index, width, offset)} = "
                    f"1'b{setup.holds[bit]};"
                )
    decl_text = "\n".join(decls)
    conn_text = ",\n".join(conns)
    hold_text = "\n".join(holds)
    # The driven inputs are single-bit ports (xcheck.analyze requires it).
    driven = ", ".join(_escaped(name) for name in columns)
    last = len(columns)
    tck = _escaped(ports.tck)
    # Each row: TMS, TDI, TRST_N (and a pulsed chip reset) for the period, then
    # whether it's a shift (sampled).
    return f"""`timescale 1ns/1ps
module faultflow_jtag_tb;
{decl_text}
    {_escaped(top)} dut (
{conn_text}
    );
    reg [{last}:0] jtag_rows [0:{len(program) - 1}];
    integer i;
    initial begin
        $readmemb("jtag_program.mem", jtag_rows);
{hold_text}
        for (i = 0; i < {len(program)}; i = i + 1) begin
            {{{driven}}} = jtag_rows[i][{last}:1];
            {tck} = 1'b0;
            #10;
            if (jtag_rows[i][0]) $display("TDO %0d %b", i, {_escaped(ports.tdo)});
            {tck} = 1'b1;
            #10;
        end
        $finish;
    end
endmodule
"""


def verify_on_gate_level(
    cfg: FaultflowConfig,
    manifest: Mapping[str, Any],
    program: TckProgram,
    setup: JtagSetup,
    golden: list[int],
) -> None:
    sky = manifest.get("sky130_verilog")
    if not isinstance(sky, str) or not Path(sky).exists():
        raise JtagVerifyError(
            "--verify needs the techmapped gate-level netlist: run 'ff.py scan-techmap'"
        )
    models = cfg.verilog_models
    if models is not None and not models.is_absolute() and not models.exists():
        models = Path(__file__).resolve().parents[2] / models  # repo-relative default
    if models is None or not models.exists():
        raise JtagVerifyError(
            "--verify needs [simulation] verilog_models (the PDK models)"
        )
    top = str(manifest["top"])
    data = json.loads(Path(str(manifest["generic_json"])).read_text(encoding="utf-8"))
    replay_on_gate_level(
        Path(sky),
        data["modules"][top],
        top,
        program,
        setup,
        golden,
        models=models,
        blackbox=set(cfg.blackbox_instances),
    )


def replay_on_gate_level(
    netlist: Path,
    module: Mapping[str, Any],
    top: str,
    program: TckProgram,
    setup: JtagSetup,
    golden: list[int],
    *,
    models: Path,
    blackbox: set[str],
) -> None:
    """Play ``program`` on the gate-level Verilog ``netlist`` (``module``: its top, from
    the netlist's JSON) with the PDK ``models`` and each ``blackbox`` instance a
    port-only stub, and require TDO to be ``golden`` at every shift."""
    for tool in ("iverilog", "vvp"):
        if shutil.which(tool) is None:
            raise JtagVerifyError(f"--verify needs {tool} on PATH")
    rows = _rows(program, setup)
    with tempfile.TemporaryDirectory(prefix="faultflow-jtag-verify-") as tmp:
        work = Path(tmp)
        (work / "jtag_program.mem").write_text("\n".join(rows) + "\n", encoding="utf-8")
        tb = work / "tb.v"
        tb.write_text(_testbench(module, top, program, setup), encoding="utf-8")
        stubs = work / "stubs.v"
        stubs.write_text(_stubs(module, blackbox), encoding="utf-8")
        sim = work / "sim.vvp"
        compiled = subprocess.run(
            [
                "iverilog",
                "-g2012",
                "-DFUNCTIONAL",
                "-o",
                str(sim),
                str(tb),
                str(netlist),
                str(stubs),
                str(models),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if compiled.returncode != 0:
            errors = [
                line
                for line in compiled.stderr.splitlines()
                if "error" in line.lower() or "syntax" in line.lower()
            ]
            raise JtagVerifyError(
                "iverilog failed:\n"
                + "\n".join(errors[:20] or [compiled.stderr[-2000:]])
            )
        run = subprocess.run(
            ["vvp", "-n", str(sim)],
            capture_output=True,
            text=True,
            check=False,
            cwd=work,
        )
    seen = [
        (int(m.group(1)), m.group(2))
        for m in re.finditer(r"^TDO (\d+) (\S+)$", run.stdout, flags=re.MULTILINE)
    ]
    periods = program.shift_periods()
    if [p for p, _ in seen] != periods:
        raise JtagVerifyError(
            f"the gate-level replay sampled {len(seen)} shifts, the program has "
            f"{len(periods)}:\n{run.stdout[-1000:]}{run.stderr[-1000:]}"
        )
    for (period, value), expected in zip(seen, golden):
        if value not in ("0", "1") or int(value) != expected:
            raise JtagVerifyError(
                f"gate-level TDO is {value} at TCK period {period} (test "
                f"{program.test_of(period)}); the two-valued golden run says {expected}"
            )
