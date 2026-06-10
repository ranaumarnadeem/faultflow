from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultflow.scan.stitch import YOSYS_SCAN_CELL_TYPE


@dataclass(frozen=True)
class ScanTechmapConfig:
    library_cell: str = "sky130_fd_sc_hd__sdfxtp_1"
    clock_pin: str = "CLK"
    data_pin: str = "D"
    scan_in_pin: str = "SCD"
    scan_enable_pin: str = "SCE"
    output_pin: str = "Q"


def render_scan_techmap(config: ScanTechmapConfig | None = None) -> str:
    cfg = config or ScanTechmapConfig()
    celltype = YOSYS_SCAN_CELL_TYPE.replace("\\", "\\\\")
    return (
        f'(* techmap_celltype = "{celltype}" *)\n'
        "module faultflow_scanff_sky130_map (\n"
        "    input CLK,\n"
        "    input D,\n"
        "    input SI,\n"
        "    input SE,\n"
        "    output Q\n"
        ");\n"
        "\n"
        f"    {cfg.library_cell} _TECHMAP_REPLACE_ (\n"
        f"        .{cfg.clock_pin}(CLK),\n"
        f"        .{cfg.data_pin}(D),\n"
        f"        .{cfg.scan_in_pin}(SI),\n"
        f"        .{cfg.scan_enable_pin}(SE),\n"
        f"        .{cfg.output_pin}(Q)\n"
        "    );\n"
        "\n"
        "endmodule\n"
    )


def write_scan_techmap(path: Path, config: ScanTechmapConfig | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_scan_techmap(config), encoding="utf-8")
    return path
