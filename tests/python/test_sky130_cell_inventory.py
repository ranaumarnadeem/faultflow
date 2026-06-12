from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from faultflow.cell_map import is_supported_or_deferred, load_cell_map

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

SKY130_DESIGNS: list[tuple[str, Path, str]] = [
    ("c17", ROOT / "tests/benchmarks/iscas85/c17.v", "c17"),
    ("c432", ROOT / "tests/benchmarks/iscas85/c432.v", "c432"),
    ("c499", ROOT / "tests/benchmarks/iscas85/c499.v", "c499"),
    ("serial_adder", ROOT / "examples/serial_adder.v", "serial_adder"),
    ("s27", ROOT / "tests/benchmarks/iscas89/s27.v", "s27_bench"),
    ("s386", ROOT / "tests/benchmarks/iscas89/s386.v", "s386_bench"),
    ("s510", ROOT / "tests/benchmarks/iscas89/s510.v", "s510_bench"),
]

YOSYS_TEMPLATE = """read_verilog {verilog}
hierarchy -check -top {top}
proc
flatten
opt_expr
opt_clean
synth -top {top}
dfflibmap -liberty {liberty}
abc -liberty {liberty}
clean
write_json {json}
"""


def _require_inventory_tools() -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    if not LIBERTY.exists():
        pytest.skip("Sky130 liberty file missing")
    if not CELL_MAP.exists():
        pytest.skip("Sky130 cell map missing")


def _synth_sky130_json(
    verilog: Path, top: str, work_dir: Path
) -> tuple[Path, list[str]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    out_json = work_dir / f"{top}.json"
    script_path = work_dir / "yosys_synth.tcl"
    log_path = work_dir / "yosys.log"
    script_path.write_text(
        YOSYS_TEMPLATE.format(
            verilog=verilog.resolve(),
            top=top,
            liberty=LIBERTY.resolve(),
            json=out_json.resolve(),
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["yosys", "-q", "-s", str(script_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"yosys synth failed for {top}; see {log_path}")
    return out_json, proc.stdout.splitlines()


def _cell_types_from_json(json_path: Path, top: str) -> set[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    module = data["modules"][top]
    return {str(cell["type"]) for cell in module.get("cells", {}).values()}


@pytest.mark.integration
@pytest.mark.parametrize("name,verilog,top", SKY130_DESIGNS)
def test_sky130_synth_cells_are_supported_or_deferred(
    tmp_path: Path, name: str, verilog: Path, top: str
) -> None:
    _require_inventory_tools()
    if not verilog.exists():
        pytest.skip(f"{name} verilog source missing")

    entries = load_cell_map(CELL_MAP)
    json_path, _ = _synth_sky130_json(verilog, top, tmp_path / name)
    emitted = _cell_types_from_json(json_path, top)

    assert emitted, f"{name} synth produced no cells"
    unsupported = sorted(
        cell_type
        for cell_type in emitted
        if not is_supported_or_deferred(cell_type, entries)
    )
    assert unsupported == [], f"{name} emitted unsupported Sky130 cells: {unsupported}"
