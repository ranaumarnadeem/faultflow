from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from faultflow.scan.stitch import (
    YOSYS_CAPTURE_AND_CELL,
    YOSYS_CAPTURE_INV_CELL,
    YOSYS_CAPTURE_OR_CELL,
    YOSYS_SCAN_CELL_TYPE,
    YOSYS_SCAN_RESET_CELL_TYPE,
    YOSYS_SCAN_SET_CELL_TYPE,
)

if TYPE_CHECKING:
    from faultflow.pdk.roles import LibraryRoles


@dataclass(frozen=True)
class ScanTechmapConfig:
    library_cell: str = "sky130_fd_sc_hd__sdfxtp_1"
    # Scan cells that keep a single async control (async-reset / async-set FFs).
    reset_cell: str = "sky130_fd_sc_hd__sdfrtp_1"
    set_cell: str = "sky130_fd_sc_hd__sdfstp_1"
    # The gates scan stitching folds an enable flop's enable into, ahead of its scan
    # cell's D (stitch._add_enable_hold_mux).
    and_cell: str = "sky130_fd_sc_hd__and2_1"
    or_cell: str = "sky130_fd_sc_hd__or2_1"
    inv_cell: str = "sky130_fd_sc_hd__inv_1"
    reset_pin: str = "RESET_B"
    set_pin: str = "SET_B"
    clock_pin: str = "CLK"
    data_pin: str = "D"
    scan_in_pin: str = "SCD"
    scan_enable_pin: str = "SCE"
    output_pin: str = "Q"
    # The gates' pins: (a, b, out) and (a, out).
    and_pins: tuple[str, str, str] = ("A", "B", "X")
    or_pins: tuple[str, str, str] = ("A", "B", "X")
    inv_pins: tuple[str, str] = ("A", "Y")
    # Each scan cell's pins held at a constant: the controls a scan flop with more of
    # them than its role needs holds inactive (faultflow.pdk.roles.RoleCell.ties).
    library_ties: tuple[tuple[str, str], ...] = ()
    reset_ties: tuple[tuple[str, str], ...] = ()
    set_ties: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_roles(cls, roles: "LibraryRoles") -> "ScanTechmapConfig":
        """The techmap onto the library ``roles`` describes (faultflow.pdk.roles).
        A scan flop with a set (or reset) the library lacks maps to nothing: its
        module is left out, and stitching such a flop leaves it unmapped."""
        from faultflow.pdk.roles import PdkError

        scan = roles["scan_ff"]
        pins = dict(scan.pins)
        optional: dict[str, dict[str, str] | None] = {}
        ties: dict[str, tuple[tuple[str, str], ...]] = {}
        for role in ("scan_ff_reset", "scan_ff_set"):
            try:
                optional[role] = dict(roles[role].pins) | {"cell": roles[role].cell}
                ties[role] = tuple(sorted(roles[role].ties.items()))
            except PdkError:
                optional[role] = None
                ties[role] = ()
        for role, found in optional.items():
            if found is not None and any(
                found[k] != pins[k] for k in ("clk", "d", "si", "se", "q")
            ):
                raise PdkError(
                    f"{roles.name}: the scan flops' pins differ ({role}); FaultFlow's "
                    "techmap needs one set of clock, data, scan and output pins"
                )
        reset, set_ = optional["scan_ff_reset"], optional["scan_ff_set"]
        and2, or2, inv = roles["and2"], roles["or2"], roles["inv"]
        return cls(
            library_cell=scan.cell,
            reset_cell=reset["cell"] if reset else "",
            set_cell=set_["cell"] if set_ else "",
            and_cell=and2.cell,
            or_cell=or2.cell,
            inv_cell=inv.cell,
            reset_pin=reset["reset_n"] if reset else "RESET_B",
            set_pin=set_["set_n"] if set_ else "SET_B",
            clock_pin=pins["clk"],
            data_pin=pins["d"],
            scan_in_pin=pins["si"],
            scan_enable_pin=pins["se"],
            output_pin=pins["q"],
            and_pins=(and2.pins["a"], and2.pins["b"], and2.pins["out"]),
            or_pins=(or2.pins["a"], or2.pins["b"], or2.pins["out"]),
            inv_pins=(inv.pins["a"], inv.pins["out"]),
            library_ties=tuple(sorted(scan.ties.items())),
            reset_ties=ties["scan_ff_reset"],
            set_ties=ties["scan_ff_set"],
        )


def _render_module(
    celltype: str,
    module_name: str,
    library_cell: str,
    cfg: ScanTechmapConfig,
    *,
    extra_port: str | None = None,
    extra_pin: str | None = None,
    ties: tuple[tuple[str, str], ...] = (),
) -> str:
    escaped = celltype.replace("\\", "\\\\")
    extra_in = f"    input {extra_port},\n" if extra_port else ""
    extra_conn = f"        .{extra_pin}({extra_port}),\n" if extra_port else ""
    extra_conn += "".join(f"        .{pin}(1'b{value}),\n" for pin, value in ties)
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


def _render_gate(
    celltype: str,
    library_cell: str,
    inputs: tuple[str, ...],
    output_pin: str,
    library_inputs: tuple[str, ...] | None = None,
) -> str:
    """A techmap module turning one of the gates scan stitching adds (inputs A[, B],
    output Y) into `library_cell`, whose pins for them are `library_inputs` (the
    same names by default) and `output_pin`."""
    escaped = celltype.replace("\\", "\\\\")
    module_name = celltype.lstrip("\\$") + "_sky130_map"
    ports = "".join(f"    input {pin},\n" for pin in inputs)
    connections = "".join(
        f"        .{lib}({pin}),\n"
        for pin, lib in zip(inputs, library_inputs or inputs)
    )
    return (
        f'(* techmap_celltype = "{escaped}" *)\n'
        f"module {module_name} (\n"
        f"{ports}"
        "    output Y\n"
        ");\n"
        "\n"
        f"    {library_cell} _TECHMAP_REPLACE_ (\n"
        f"{connections}"
        f"        .{output_pin}(Y)\n"
        "    );\n"
        "\n"
        "endmodule\n"
    )


def render_scan_techmap(config: ScanTechmapConfig | None = None) -> str:
    """The Yosys techmap from FaultFlow's scan netlist to sky130 cells: its scan
    cells, and the gates an enable flop's enable is folded into."""
    cfg = config or ScanTechmapConfig()
    gates = (
        (YOSYS_CAPTURE_AND_CELL, cfg.and_cell, ("A", "B"), cfg.and_pins),
        (YOSYS_CAPTURE_OR_CELL, cfg.or_cell, ("A", "B"), cfg.or_pins),
        (YOSYS_CAPTURE_INV_CELL, cfg.inv_cell, ("A",), cfg.inv_pins),
    )
    gate_maps = "".join(
        "\n" + _render_gate(celltype, cell, inputs, pins[-1], pins[:-1])
        for celltype, cell, inputs, pins in gates
    )
    # The generic reset / set scan cells' control port is RESET_B / SET_B
    # (faultflow.scan.stitch); the library cell's pin is the config's.
    text = _render_module(
        YOSYS_SCAN_CELL_TYPE,
        "faultflow_scanff_sky130_map",
        cfg.library_cell,
        cfg,
        ties=cfg.library_ties,
    )
    if cfg.reset_cell:
        text += "\n" + _render_module(
            YOSYS_SCAN_RESET_CELL_TYPE,
            "faultflow_scanff_r_sky130_map",
            cfg.reset_cell,
            cfg,
            extra_port="RESET_B",
            extra_pin=cfg.reset_pin,
            ties=cfg.reset_ties,
        )
    if cfg.set_cell:
        text += "\n" + _render_module(
            YOSYS_SCAN_SET_CELL_TYPE,
            "faultflow_scanff_s_sky130_map",
            cfg.set_cell,
            cfg,
            extra_port="SET_B",
            extra_pin=cfg.set_pin,
            ties=cfg.set_ties,
        )
    return text + gate_maps


def scan_techmap_config(cell_map: Path) -> ScanTechmapConfig:
    """The scan techmap onto the library of ``cell_map`` (faultflow.pdk.roles). A
    library with no scan flop (OSU035) keeps the default: its flows never techmap
    (TechnologyProfile.supports_physical_scan)."""
    from faultflow.pdk.roles import PdkError, library_roles

    loaded = json.loads(Path(cell_map).read_text(encoding="utf-8"))
    try:
        return ScanTechmapConfig.from_roles(library_roles(loaded))
    except PdkError:
        return ScanTechmapConfig()


def write_scan_techmap(path: Path, config: ScanTechmapConfig | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_scan_techmap(config), encoding="utf-8")
    return path
