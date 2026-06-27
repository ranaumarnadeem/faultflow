from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultflow.scan.stitch import (
    YOSYS_SCAN_CELL_TYPE,
    YOSYS_SCAN_RESET_CELL_TYPE,
    YOSYS_SCAN_SET_CELL_TYPE,
)


@dataclass(frozen=True)
class ScanTechmapConfig:
    library_cell: str = "sky130_fd_sc_hd__sdfxtp_1"
    # Scan cells that keep a single async control (async-reset / async-set FFs).
    reset_cell: str = "sky130_fd_sc_hd__sdfrtp_1"
    set_cell: str = "sky130_fd_sc_hd__sdfstp_1"
    reset_pin: str = "RESET_B"
    set_pin: str = "SET_B"
    clock_pin: str = "CLK"
    data_pin: str = "D"
    scan_in_pin: str = "SCD"
    scan_enable_pin: str = "SCE"
    output_pin: str = "Q"


def _render_module(
    celltype: str,
    module_name: str,
    library_cell: str,
    cfg: ScanTechmapConfig,
    *,
    extra_port: str | None = None,
    extra_pin: str | None = None,
) -> str:
    escaped = celltype.replace("\\", "\\\\")
    extra_in = f"    input {extra_port},\n" if extra_port else ""
    extra_conn = f"        .{extra_pin}({extra_port}),\n" if extra_port else ""
    return (
        f'(* techmap_celltype = "{escaped}" *)\n'
        f"module {module_name} (\n"
        "    input CLK,\n"
        "    input D,\n"
        "    input SDI,\n"
        "    input SE,\n"
        f"{extra_in}"
        "    output Q\n"
        ");\n"
        "\n"
        f"    {library_cell} _TECHMAP_REPLACE_ (\n"
        f"        .{cfg.clock_pin}(CLK),\n"
        f"        .{cfg.data_pin}(D),\n"
        f"        .{cfg.scan_in_pin}(SDI),\n"
        f"        .{cfg.scan_enable_pin}(SE),\n"
        f"{extra_conn}"
        f"        .{cfg.output_pin}(Q)\n"
        "    );\n"
        "\n"
        "endmodule\n"
    )


def render_scan_techmap(config: ScanTechmapConfig | None = None) -> str:
    cfg = config or ScanTechmapConfig()
    return (
        _render_module(
            YOSYS_SCAN_CELL_TYPE,
            "faultflow_scanff_sky130_map",
            cfg.library_cell,
            cfg,
        )
        + "\n"
        + _render_module(
            YOSYS_SCAN_RESET_CELL_TYPE,
            "faultflow_scanff_r_sky130_map",
            cfg.reset_cell,
            cfg,
            extra_port=cfg.reset_pin,
            extra_pin=cfg.reset_pin,
        )
        + "\n"
        + _render_module(
            YOSYS_SCAN_SET_CELL_TYPE,
            "faultflow_scanff_s_sky130_map",
            cfg.set_cell,
            cfg,
            extra_port=cfg.set_pin,
            extra_pin=cfg.set_pin,
        )
    )


def write_scan_techmap(path: Path, config: ScanTechmapConfig | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_scan_techmap(config), encoding="utf-8")
    return path
