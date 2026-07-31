from __future__ import annotations

import copy
import dataclasses
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError

SCAN_CELL_TYPE = "$scanff_faultflow"
YOSYS_SCAN_CELL_TYPE = "\\$scanff_faultflow"
SCAN_CELL_PINS = ("CLK", "D", "SDI", "SE", "Q")
# Scan cells that preserve a single async control, for stitching async-reset/set
# FFs. Techmapped to sky130 sdfrtp_1 / sdfstp_1 (see faultflow/scan/techmap.py).
SCAN_RESET_CELL_TYPE = "$scanff_r_faultflow"
YOSYS_SCAN_RESET_CELL_TYPE = "\\$scanff_r_faultflow"
SCAN_SET_CELL_TYPE = "$scanff_s_faultflow"
YOSYS_SCAN_SET_CELL_TYPE = "\\$scanff_s_faultflow"

# Generic gate cells used to synthesize a clock-enable hold-mux ahead of a
# scan-replacement FF's D pin (see _add_enable_hold_mux). The generic scan
# cell ($scanff_faultflow) has no DE pin of its own -- only CLK/D/SDI/SE/Q --
# so an FF's enable behavior must be folded into its D input structurally.
# Reuses the same synthetic gate types atpg_view.py's async-control mux uses.
YOSYS_CAPTURE_AND_CELL = "\\$faultflow_capture_and"
YOSYS_CAPTURE_OR_CELL = "\\$faultflow_capture_or"
YOSYS_CAPTURE_INV_CELL = "\\$faultflow_capture_inv"

# Every faultflow generic scan-cell type (plain + async reset/set), in both the
# bare and Yosys-escaped spellings. Use this wherever scan cells are identified
# so the async-reset/set variants are recognized alongside the plain cell.
SCAN_CELL_TYPES = frozenset(
    {
        SCAN_CELL_TYPE,
        YOSYS_SCAN_CELL_TYPE,
        SCAN_RESET_CELL_TYPE,
        YOSYS_SCAN_RESET_CELL_TYPE,
        SCAN_SET_CELL_TYPE,
        YOSYS_SCAN_SET_CELL_TYPE,
    }
)
DEFAULT_SCAN_IN = "scan_in"
DEFAULT_SCAN_OUT = "scan_out"
DEFAULT_SCAN_ENABLE = "scan_en"

# Attributes set by wrap_ports on WBR scan cells; used to collect the wrapper chain.
WBR_SCAN_ATTR = "faultflow_wbr"
WBR_CHAIN_ATTR = "faultflow_wbr_chain"
WBR_BIT_ATTR = "wbr_bit"
DEFAULT_WBR_SCAN_IN = "wbr_si"
DEFAULT_WBR_SCAN_OUT = "wbr_so"

INELIGIBLE_REASONS = {
    "has_async_reset",
    "has_async_set",
    "has_async_reset_set",
    "negedge_clock",
    "no_clock_pin",
    "no_data_pin",
    "no_output_pin",
    "unknown_cell_type",
    "existing_scan_cell",
    "wbr_scan_cell",
    "unsupported_ff_shape",
    "multiple_clock_nets",
}


@dataclass(frozen=True)
class ScanCellRecord:
    instance: str
    original_type: str
    chain_index: int
    chain_position: int
    clock_net: int
    data_net: int
    scan_in_net: int
    scan_enable_net: int
    q_net: int


@dataclass(frozen=True)
class IneligibleFF:
    instance: str
    cell_type: str
    reason: str


@dataclass(frozen=True)
class ScanChainRecord:
    index: int
    scan_in: str
    scan_out: str
    scan_in_net: int
    scan_out_net: int
    cells: list[ScanCellRecord]

    @property
    def length(self) -> int:
        return len(self.cells)


@dataclass(frozen=True)
class ScanPlan:
    top: str
    chain_count: int
    cell_count: int
    clock_nets: list[int]
    scan_inputs: list[str]
    scan_outputs: list[str]
    scan_enable: str
    chains: list[ScanChainRecord]
    cells: list[ScanCellRecord]
    ineligible_ffs: list[IneligibleFF]
    wrapper_chains: list[ScanChainRecord]  # WBR scan cells; empty if wbr_model=buffer


@dataclass(frozen=True)
class ScanStitchResult(ScanPlan):
    output_json: Path


@dataclass(frozen=True)
class _EligibleFF:
    instance: str
    cell: dict[str, Any]
    ff_meta: dict[str, Any]
    original_type: str
    clock_net: int
    data_net: int
    q_net: int
    # Single async control to preserve through scan stitching: "none" | "reset" |
    # "set". async_net is the control net (-1 if none); async_pin is the cell pin
    # (RESET_B / SET_B) the scan-cell variant wires it to.
    async_kind: str = "none"
    async_net: int = -1
    async_pin: str | None = None
    # Clock-enable (e.g. sky130 edfxtp's DE) to preserve through scan stitching.
    # enable_net is the control net (-1 if none); enable_level is "HIGH" or "LOW"
    # (which value of the enable net means "load D", per the cell-map ff.enable
    # spec). The generic scan cell has no enable pin, so stitch_scan_json
    # synthesizes a hold-mux ahead of D instead of wiring this directly.
    enable_net: int = -1
    enable_level: str = "HIGH"


@dataclass(frozen=True)
class _WBRCell:
    """One WBR scan boundary cell, ordered into a wrapper chain."""

    instance: str
    cell_type: str
    chain_group: int  # faultflow_wbr_chain attribute
    bit_pos: int  # wbr_bit attribute
    clock_net: int
    cti_net: int  # CTI — scan chain in
    cto_net: int  # CTO — scan chain out (= q)
    se_net: int
    func_net: int  # FROM_SYS / FROM_CORE; -1 if absent


def _truthy_attr(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if set(text) <= {"0", "1"}:
        return "1" in text
    return False


def _top_module(data: dict[str, Any], requested_top: str) -> tuple[str, dict[str, Any]]:
    modules = data.get("modules")
    if not isinstance(modules, dict):
        raise ScanError("Yosys JSON is missing modules")
    if requested_top in modules:
        module = modules[requested_top]
        if not isinstance(module, dict):
            raise ScanError(f"module {requested_top} is malformed")
        return requested_top, module
    for name, module in modules.items():
        if not isinstance(module, dict):
            continue
        attrs = module.get("attributes", {})
        if isinstance(attrs, dict) and _truthy_attr(attrs.get("top", False)):
            return str(name), module
    raise ScanError(f"Cannot find top module {requested_top}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScanError(f"Cannot parse JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScanError(f"JSON root must be an object: {path}")
    return data


def _lookup_cell(
    cell_map: dict[str, Any], cell_type: str
) -> tuple[str, dict[str, Any]] | None:
    exact = cell_map.get(cell_type)
    if isinstance(exact, dict):
        return cell_type, exact

    matches: list[tuple[int, str, dict[str, Any]]] = []
    for pattern, entry in cell_map.items():
        if not isinstance(entry, dict):
            continue
        if fnmatch.fnmatchcase(cell_type, pattern):
            matches.append((len(pattern.rstrip("*")), pattern, entry))
    if not matches:
        return None
    _, pattern, entry = max(matches, key=lambda item: (item[0], item[1]))
    return pattern, entry


def _one_bit(value: Any, context: str) -> int:
    if not isinstance(value, list) or len(value) != 1:
        raise ScanError(f"{context} must be exactly one bit")
    bit = value[0]
    if not isinstance(bit, int):
        raise ScanError(f"{context} must be a concrete net bit, got {bit!r}")
    return bit


def _maybe_one_bit(value: Any) -> int | None:
    if not isinstance(value, list) or len(value) != 1:
        return None
    bit = value[0]
    return bit if isinstance(bit, int) else None


def _ff_pin(ff_meta: dict[str, Any], key: str) -> str | None:
    value = ff_meta.get(key)
    return value if isinstance(value, str) and value else None


def _async_control(ff_meta: dict[str, Any]) -> tuple[str, str | None]:
    """The single async control of an FF as (kind, pin): ("reset", "RESET_B"),
    ("set", "SET_B"), or ("none", None). Reads the cell-map `ff:` clear/preset
    metadata (a {"pin": ...} dict, or a bare pin string)."""
    for key, kind in (
        ("clear", "reset"),
        ("reset", "reset"),
        ("preset", "set"),
        ("set", "set"),
    ):
        spec = ff_meta.get(key)
        if spec is None:
            continue
        if isinstance(spec, dict):
            pin = spec.get("pin")
            return kind, pin if isinstance(pin, str) and pin else None
        if isinstance(spec, str) and spec:
            return kind, spec
        return kind, None
    return "none", None


def _enable_control(ff_meta: dict[str, Any]) -> tuple[str | None, str]:
    """The clock-enable of an FF as (pin, level): reads the cell-map `ff:`
    enable metadata ({"pin": "DE", "level": "HIGH"|"LOW"}, e.g. sky130
    edfxtp). Returns (None, "HIGH") if the FF has no enable. `level` is which
    value of the enable net means "load D" (HIGH if unspecified)."""
    spec = ff_meta.get("enable")
    if not isinstance(spec, dict):
        return None, "HIGH"
    pin = spec.get("pin")
    if not isinstance(pin, str) or not pin:
        return None, "HIGH"
    level = spec.get("level")
    return pin, level if level in ("HIGH", "LOW") else "HIGH"


def _all_int_bits(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [bit for bit in value if isinstance(bit, int)]


def _next_net_id(module: dict[str, Any]) -> int:
    max_id = -1
    for port in module.get("ports", {}).values():
        if isinstance(port, dict):
            max_id = max(max_id, *(_all_int_bits(port.get("bits")) or [-1]))
    for net in module.get("netnames", {}).values():
        if isinstance(net, dict):
            max_id = max(max_id, *(_all_int_bits(net.get("bits")) or [-1]))
    for cell in module.get("cells", {}).values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for bits in conns.values():
            max_id = max(max_id, *(_all_int_bits(bits) or [-1]))
    return max_id + 1


def _name_set(module: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("ports", "netnames"):
        value = module.get(key, {})
        if isinstance(value, dict):
            names.update(str(name) for name in value)
    return names


def _check_names_free(module: dict[str, Any], names: list[str]) -> None:
    existing = _name_set(module)
    for name in names:
        if name in existing:
            raise ScanError(f"scan artifact name already exists: {name}")
    if len(set(names)) != len(names):
        raise ScanError("generated scan port names are not unique")


def _add_port(module: dict[str, Any], name: str, direction: str, bit: int) -> None:
    module.setdefault("ports", {})[name] = {"direction": direction, "bits": [bit]}
    module.setdefault("netnames", {})[name] = {
        "hide_name": 0,
        "bits": [bit],
        "attributes": {},
    }


def _is_ffish(cell_type: str) -> bool:
    lowered = cell_type.lower()
    return "dff" in lowered or lowered.endswith("ff") or "scanff" in lowered


def _reason_for_ff(
    instance: str,
    cell: dict[str, Any],
    ff_meta: dict[str, Any],
) -> str | None:
    del instance
    conns = cell.get("connections", {})
    if not isinstance(conns, dict):
        return "unsupported_ff_shape"

    if "scan" in ff_meta:
        return "existing_scan_cell"
    has_clear = "clear" in ff_meta or "reset" in ff_meta
    has_preset = "preset" in ff_meta or "set" in ff_meta
    # A single async reset OR set is now scannable: stitching maps it to a scan
    # cell that keeps the control net (sdfrtp/sdfstp) and the sim gates the
    # control off during shift. Reset AND set together (dfbb*) has no sky130 scan
    # cell, so it stays ineligible; the control pin must be a single net.
    if has_clear and has_preset:
        return "has_async_reset_set"
    if has_clear or has_preset:
        _, pin = _async_control(ff_meta)
        if pin is None or _maybe_one_bit(conns.get(pin)) is None:
            return "unsupported_ff_shape"
    if "enable" in ff_meta:
        enable_pin, _ = _enable_control(ff_meta)
        if enable_pin is None or _maybe_one_bit(conns.get(enable_pin)) is None:
            return "unsupported_ff_shape"
    if ff_meta.get("trigger") == "NEGEDGE":
        return "negedge_clock"
    if ff_meta.get("trigger") != "POSEDGE":
        return "unsupported_ff_shape"

    for key, reason in (
        ("clock", "no_clock_pin"),
        ("data", "no_data_pin"),
        ("output", "no_output_pin"),
    ):
        pin = _ff_pin(ff_meta, key)
        if pin is None or _maybe_one_bit(conns.get(pin)) is None:
            return reason
    return None


def _collect_ffs(
    module: dict[str, Any], cell_map: dict[str, Any]
) -> tuple[list[_EligibleFF], list[IneligibleFF]]:
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    eligible: list[_EligibleFF] = []
    ineligible: list[IneligibleFF] = []
    for instance, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            raise ScanError(f"{instance}: cell entry is malformed")
        cell_type = str(cell.get("type", ""))
        if cell_type in SCAN_CELL_TYPES:
            ineligible.append(
                IneligibleFF(str(instance), cell_type, "existing_scan_cell")
            )
            continue
        # WBR scan cells ($wbc_*_scan_faultflow) form the wrapper chain — skip here.
        attrs = cell.get("attributes", {})
        if isinstance(attrs, dict) and WBR_SCAN_ATTR in attrs:
            ineligible.append(IneligibleFF(str(instance), cell_type, "wbr_scan_cell"))
            continue
        match = _lookup_cell(cell_map, cell_type)
        if match is None:
            if _is_ffish(cell_type):
                ineligible.append(
                    IneligibleFF(str(instance), cell_type, "unknown_cell_type")
                )
            continue
        _, entry = match
        if entry.get("node_type") != "FF":
            continue
        ff_meta = entry.get("ff")
        if not isinstance(ff_meta, dict):
            ineligible.append(
                IneligibleFF(str(instance), cell_type, "unsupported_ff_shape")
            )
            continue
        reason = _reason_for_ff(str(instance), cell, ff_meta)
        if reason is not None:
            ineligible.append(IneligibleFF(str(instance), cell_type, reason))
            continue

        conns = cell["connections"]
        clk_pin = _ff_pin(ff_meta, "clock")
        d_pin = _ff_pin(ff_meta, "data")
        q_pin = _ff_pin(ff_meta, "output")
        if clk_pin is None or d_pin is None or q_pin is None:
            raise ScanError(f"{instance}: internal FF metadata validation failed")
        async_kind, async_pin = _async_control(ff_meta)
        async_net = (
            _one_bit(conns.get(async_pin), f"{instance}.{async_pin}")
            if async_kind != "none" and async_pin is not None
            else -1
        )
        enable_pin, enable_level = _enable_control(ff_meta)
        enable_net = (
            _one_bit(conns.get(enable_pin), f"{instance}.{enable_pin}")
            if enable_pin is not None
            else -1
        )
        eligible.append(
            _EligibleFF(
                instance=str(instance),
                cell=cell,
                ff_meta=ff_meta,
                original_type=cell_type,
                clock_net=_one_bit(conns.get(clk_pin), f"{instance}.{clk_pin}"),
                data_net=_one_bit(conns.get(d_pin), f"{instance}.{d_pin}"),
                q_net=_one_bit(conns.get(q_pin), f"{instance}.{q_pin}"),
                async_kind=async_kind,
                async_net=async_net,
                async_pin=async_pin,
                enable_net=enable_net,
                enable_level=enable_level,
            )
        )
    return eligible, ineligible


def scan_port_names(
    chain_count: int,
    scan_in_base: str = DEFAULT_SCAN_IN,
    scan_out_base: str = DEFAULT_SCAN_OUT,
) -> tuple[list[str], list[str]]:
    if chain_count < 1:
        raise ScanError("scan_chains must be >= 1")
    if chain_count == 1:
        return [scan_in_base], [scan_out_base]
    return (
        [f"{scan_in_base}_{index}" for index in range(chain_count)],
        [f"{scan_out_base}_{index}" for index in range(chain_count)],
    )


def balanced_chain_lengths(ff_count: int, chain_count: int) -> list[int]:
    if chain_count < 1:
        raise ScanError("scan_chains must be >= 1")
    if ff_count < 1:
        raise ScanError("no eligible FF cells found to stitch")
    if chain_count > ff_count:
        raise ScanError("scan_chains cannot exceed eligible FF count")
    base, extra = divmod(ff_count, chain_count)
    return [base + (1 if index < extra else 0) for index in range(chain_count)]


def _chain_count_from_options(
    ff_count: int, scan_chains: int, max_chain_length: int | None
) -> int:
    if scan_chains < 1:
        raise ScanError("scan_chains must be >= 1")
    if max_chain_length is not None and max_chain_length < 1:
        raise ScanError("max_chain_length must be >= 1")
    if (
        max_chain_length is not None
        and scan_chains == 1
        and ff_count > max_chain_length
    ):
        scan_chains = (ff_count + max_chain_length - 1) // max_chain_length
    return scan_chains


def _build_plan(
    top: str,
    module: dict[str, Any],
    eligible: list[_EligibleFF],
    ineligible: list[IneligibleFF],
    scan_chains: int,
    max_chain_length: int | None,
    scan_in_base: str,
    scan_out_base: str,
    scan_enable: str,
    scan_enable_bit: int,
    scan_in_bits: list[int],
) -> ScanPlan:
    if not eligible:
        if ineligible:
            reasons = ", ".join(sorted({ff.reason for ff in ineligible}))
            raise ScanError(f"no eligible FF cells found to stitch; reasons: {reasons}")
        raise ScanError("no eligible FF cells found to stitch")
    unique_clock_nets = sorted({ff.clock_net for ff in eligible})
    # Sort FFs by (clock_net, instance) so all FFs of each domain are adjacent,
    # which guarantees chain boundaries fall between domains (not across them).
    eligible = sorted(eligible, key=lambda ff: (ff.clock_net, ff.instance))

    chain_count = _chain_count_from_options(
        len(eligible), scan_chains, max_chain_length
    )
    if chain_count < len(unique_clock_nets):
        raise ScanError(
            f"scan_chains ({chain_count}) must be >= number of clock domains "
            f"({len(unique_clock_nets)}) so each domain gets at least one chain"
        )
    lengths = balanced_chain_lengths(len(eligible), chain_count)
    if max_chain_length is not None:
        for length in lengths:
            if length > max_chain_length:
                raise ScanError(
                    f"computed chain length {length} exceeds max_chain_length "
                    f"{max_chain_length}"
                )

    scan_inputs, scan_outputs = scan_port_names(
        chain_count, scan_in_base, scan_out_base
    )
    _check_names_free(module, [*scan_inputs, *scan_outputs, scan_enable])

    records: list[ScanCellRecord] = []
    chains: list[ScanChainRecord] = []
    cursor = 0
    for chain_index, length in enumerate(lengths):
        previous_q = scan_in_bits[chain_index]
        chain_cells: list[ScanCellRecord] = []
        chain_ffs = eligible[cursor : cursor + length]
        chain_clock_nets = {ff.clock_net for ff in chain_ffs}
        if len(chain_clock_nets) > 1:
            raise ScanError(
                f"chain {chain_index} spans multiple clock domains "
                f"({sorted(chain_clock_nets)}); increase scan_chains so each "
                f"domain occupies complete chains"
            )
        for chain_position, ff in enumerate(chain_ffs):
            record = ScanCellRecord(
                instance=ff.instance,
                original_type=ff.original_type,
                chain_index=chain_index,
                chain_position=chain_position,
                clock_net=ff.clock_net,
                data_net=ff.data_net,
                scan_in_net=previous_q,
                scan_enable_net=scan_enable_bit,
                q_net=ff.q_net,
            )
            chain_cells.append(record)
            records.append(record)
            previous_q = ff.q_net
        chains.append(
            ScanChainRecord(
                index=chain_index,
                scan_in=scan_inputs[chain_index],
                scan_out=scan_outputs[chain_index],
                scan_in_net=scan_in_bits[chain_index],
                scan_out_net=previous_q,
                cells=chain_cells,
            )
        )
        cursor += length

    return ScanPlan(
        top=top,
        chain_count=chain_count,
        cell_count=len(records),
        clock_nets=unique_clock_nets,
        scan_inputs=scan_inputs,
        scan_outputs=scan_outputs,
        scan_enable=scan_enable,
        chains=chains,
        cells=records,
        ineligible_ffs=ineligible,
        wrapper_chains=[],  # populated by callers after _collect_wbr_cells
    )


def _collect_wbr_cells(
    module: dict[str, Any], cell_map: dict[str, Any]
) -> list[_WBRCell]:
    """Collect WBR scan cells (tagged faultflow_wbr), ordered by chain group + bit."""
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return []
    result: list[_WBRCell] = []
    for instance, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            continue
        attrs = cell.get("attributes", {})
        if not isinstance(attrs, dict) or WBR_SCAN_ATTR not in attrs:
            continue
        cell_type = str(cell.get("type", ""))
        match = _lookup_cell(cell_map, cell_type)
        if match is None:
            raise ScanError(
                f"{instance}: WBR scan cell {cell_type!r} not found in cell map"
            )
        _, entry = match
        wbr = entry.get("wbr", {})
        if not isinstance(wbr, dict) or not wbr.get("scan"):
            raise ScanError(
                f"{instance}: {cell_type!r} does not have wbr.scan=true in cell map"
            )
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            raise ScanError(f"{instance}: WBR scan cell has no connections")
        cti_pin = str(wbr.get("chain_in_pin", "CTI"))
        cto_pin = str(wbr.get("chain_out_pin", "CTO"))
        se_pin = str(wbr.get("scan_enable_pin", "SE"))
        clk_pin = str(wbr.get("clock_pin", "CLK"))
        func_pin = str(wbr.get("func_in_pin", "FROM_SYS"))
        chain_group = int(str(attrs.get(WBR_CHAIN_ATTR, "0")))
        bit_pos = int(str(attrs.get(WBR_BIT_ATTR, "0")))
        func_bits = conns.get(func_pin)
        func_net = _maybe_one_bit(func_bits) if func_bits is not None else None
        result.append(
            _WBRCell(
                instance=str(instance),
                cell_type=cell_type,
                chain_group=chain_group,
                bit_pos=bit_pos,
                clock_net=_one_bit(conns.get(clk_pin), f"{instance}.{clk_pin}"),
                cti_net=_one_bit(conns.get(cti_pin), f"{instance}.{cti_pin}"),
                cto_net=_one_bit(conns.get(cto_pin), f"{instance}.{cto_pin}"),
                se_net=_one_bit(conns.get(se_pin), f"{instance}.{se_pin}"),
                func_net=func_net if func_net is not None else -1,
            )
        )
    return sorted(result, key=lambda c: (c.chain_group, c.bit_pos))


def _build_wrapper_chains(
    module: dict[str, Any],
    wbr_cells: list[_WBRCell],
    wbr_scan_in_base: str = DEFAULT_WBR_SCAN_IN,
    wbr_scan_out_base: str = DEFAULT_WBR_SCAN_OUT,
) -> list[ScanChainRecord]:
    """Build wrapper ScanChainRecords from ordered WBR cells.

    The wrapper chain ports (wbr_si / wbr_so) are created by wrap_ports and must
    already exist in the module — this function reads them, not creates them.
    """
    if not wbr_cells:
        return []
    groups: dict[int, list[_WBRCell]] = {}
    for cell in wbr_cells:
        groups.setdefault(cell.chain_group, []).append(cell)
    ports = module.get("ports", {})
    ports = ports if isinstance(ports, dict) else {}
    num_groups = len(groups)
    chains: list[ScanChainRecord] = []
    for group_idx in sorted(groups):
        group = sorted(groups[group_idx], key=lambda c: c.bit_pos)
        si_name = (
            wbr_scan_in_base if num_groups == 1 else f"{wbr_scan_in_base}_{group_idx}"
        )
        so_name = (
            wbr_scan_out_base if num_groups == 1 else f"{wbr_scan_out_base}_{group_idx}"
        )
        si_port = ports.get(si_name)
        si_net = (
            _maybe_one_bit(si_port.get("bits")) if isinstance(si_port, dict) else None
        )
        if si_net is None:
            raise ScanError(
                f"wrapper scan-in port {si_name!r} not found in module; "
                "run wrap_ports with wbr_model='scan' first"
            )
        records: list[ScanCellRecord] = []
        for pos, cell in enumerate(group):
            records.append(
                ScanCellRecord(
                    instance=cell.instance,
                    original_type=cell.cell_type,
                    chain_index=group_idx,
                    chain_position=pos,
                    clock_net=cell.clock_net,
                    data_net=cell.func_net if cell.func_net >= 0 else 0,
                    scan_in_net=cell.cti_net,
                    scan_enable_net=cell.se_net,
                    q_net=cell.cto_net,
                )
            )
        chains.append(
            ScanChainRecord(
                index=group_idx,
                scan_in=si_name,
                scan_out=so_name,
                scan_in_net=si_net,
                scan_out_net=group[-1].cto_net,
                cells=records,
            )
        )
    return chains


def plan_scan_json(
    netlist_json: Path,
    cell_map_json: Path,
    top: str,
    scan_chains: int = 1,
    max_chain_length: int | None = None,
    scan_in_base: str = DEFAULT_SCAN_IN,
    scan_out_base: str = DEFAULT_SCAN_OUT,
    scan_enable: str = DEFAULT_SCAN_ENABLE,
) -> ScanPlan:
    data = _load_json(netlist_json)
    cell_map = _load_json(cell_map_json)
    top_name, module = _top_module(data, top)
    eligible, ineligible = _collect_ffs(module, cell_map)
    chain_count = _chain_count_from_options(
        len(eligible), scan_chains, max_chain_length
    )
    next_id = _next_net_id(module)
    scan_in_bits = [next_id + index for index in range(chain_count)]
    scan_enable_bit = next_id + chain_count
    plan = _build_plan(
        top=top_name,
        module=module,
        eligible=eligible,
        ineligible=ineligible,
        scan_chains=scan_chains,
        max_chain_length=max_chain_length,
        scan_in_base=scan_in_base,
        scan_out_base=scan_out_base,
        scan_enable=scan_enable,
        scan_enable_bit=scan_enable_bit,
        scan_in_bits=scan_in_bits,
    )
    wbr_cells = _collect_wbr_cells(module, cell_map)
    wrapper_chains = _build_wrapper_chains(module, wbr_cells)
    return dataclasses.replace(plan, wrapper_chains=wrapper_chains)


def _add_internal_buf1_cell(
    cells: dict[str, Any], instance: str, cell_type: str, in_bit: int, out_bit: int
) -> None:
    cells[instance] = {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [in_bit], "Y": [out_bit]},
    }


def _add_internal_gate2_cell(
    cells: dict[str, Any],
    instance: str,
    cell_type: str,
    a_bit: int,
    b_bit: int,
    out_bit: int,
) -> None:
    cells[instance] = {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "B": "input", "Y": "output"},
        "connections": {"A": [a_bit], "B": [b_bit], "Y": [out_bit]},
    }


def _add_enable_hold_mux(
    cells: dict[str, Any],
    instance: str,
    d_net: int,
    q_net: int,
    enable_net: int,
    enable_level: str,
    next_id: int,
) -> tuple[int, int]:
    """Synthesize muxed_d = (enable active) ? D : Q ahead of a scan-replacement
    FF's D pin, since the generic scan cell has no enable pin of its own (see
    cells/sky130/sky130_fd_sc_hd.json's $scanff_faultflow: only CLK/D/SDI/SE).
    Decomposed into INV/AND2/OR2 -- the same synthetic gate types
    atpg_view.py's async-control mux uses -- rather than a Sky130 mux2 cell,
    to keep this on the most foundational, extensively-tested gate types.

    Returns (muxed_d_net, next_id).
    """
    not_enable_net = next_id
    next_id += 1
    _add_internal_buf1_cell(
        cells,
        f"$ffenmux_inv_{instance}",
        YOSYS_CAPTURE_INV_CELL,
        enable_net,
        not_enable_net,
    )
    d_gate_signal = enable_net if enable_level == "HIGH" else not_enable_net
    q_gate_signal = not_enable_net if enable_level == "HIGH" else enable_net

    d_gated = next_id
    next_id += 1
    _add_internal_gate2_cell(
        cells,
        f"$ffenmux_d_{instance}",
        YOSYS_CAPTURE_AND_CELL,
        d_net,
        d_gate_signal,
        d_gated,
    )
    q_gated = next_id
    next_id += 1
    _add_internal_gate2_cell(
        cells,
        f"$ffenmux_q_{instance}",
        YOSYS_CAPTURE_AND_CELL,
        q_net,
        q_gate_signal,
        q_gated,
    )
    muxed_d = next_id
    next_id += 1
    _add_internal_gate2_cell(
        cells,
        f"$ffenmux_or_{instance}",
        YOSYS_CAPTURE_OR_CELL,
        d_gated,
        q_gated,
        muxed_d,
    )
    return muxed_d, next_id


def stitch_scan_json(
    netlist_json: Path,
    cell_map_json: Path,
    top: str,
    output_json: Path,
    scan_chains: int = 1,
    max_chain_length: int | None = None,
    scan_in_base: str = DEFAULT_SCAN_IN,
    scan_out_base: str = DEFAULT_SCAN_OUT,
    scan_enable: str = DEFAULT_SCAN_ENABLE,
) -> ScanStitchResult:
    data = _load_json(netlist_json)
    cell_map = _load_json(cell_map_json)
    top_name, module = _top_module(data, top)
    stitched = copy.deepcopy(data)
    _, stitched_module = _top_module(stitched, top_name)

    eligible, ineligible = _collect_ffs(stitched_module, cell_map)
    chain_count = _chain_count_from_options(
        len(eligible), scan_chains, max_chain_length
    )
    next_id = _next_net_id(stitched_module)
    scan_in_bits = [next_id + index for index in range(chain_count)]
    scan_enable_bit = next_id + chain_count
    plan = _build_plan(
        top=top_name,
        module=stitched_module,
        eligible=eligible,
        ineligible=ineligible,
        scan_chains=scan_chains,
        max_chain_length=max_chain_length,
        scan_in_base=scan_in_base,
        scan_out_base=scan_out_base,
        scan_enable=scan_enable,
        scan_enable_bit=scan_enable_bit,
        scan_in_bits=scan_in_bits,
    )
    # Collect wrapper chain from WBR scan cells (already skipped by _collect_ffs).
    # _collect_wbr_cells reads the pre-existing wbr_si/wbr_so ports from wrap_ports.
    wbr_cells = _collect_wbr_cells(stitched_module, cell_map)
    wrapper_chains = _build_wrapper_chains(stitched_module, wbr_cells)

    for chain in plan.chains:
        _add_port(stitched_module, chain.scan_in, "input", chain.scan_in_net)
    _add_port(stitched_module, plan.scan_enable, "input", scan_enable_bit)

    cells = stitched_module["cells"]
    by_instance = {ff.instance: ff for ff in eligible}
    # Running counter for enable-hold-mux nets (see _add_enable_hold_mux) --
    # NOT re-scanned per FF (_next_net_id is O(module size); this loop can
    # touch thousands of FFs on real designs).
    mux_next_id = scan_enable_bit + 1
    for record in plan.cells:
        ff = by_instance[record.instance]
        attrs = dict(ff.cell.get("attributes", {}))
        attrs.update(
            {
                "faultflow_original_type": record.original_type,
                "faultflow_scan": "1",
                "faultflow_scan_chain": str(record.chain_index),
                "faultflow_scan_index": str(record.chain_position),
            }
        )
        port_directions = {
            "CLK": "input",
            "D": "input",
            "SDI": "input",
            "SE": "input",
            "Q": "output",
        }
        data_net = record.data_net
        if ff.enable_net != -1:
            # Generic scan cell has no enable pin -- synthesize the hold-mux
            # ahead of D instead of dropping the enable behavior entirely.
            data_net, mux_next_id = _add_enable_hold_mux(
                cells,
                record.instance,
                record.data_net,
                record.q_net,
                ff.enable_net,
                ff.enable_level,
                mux_next_id,
            )
        connections = {
            "CLK": [record.clock_net],
            "D": [data_net],
            "SDI": [record.scan_in_net],
            "SE": [record.scan_enable_net],
            "Q": [record.q_net],
        }
        # Preserve a single async control by emitting the matching scan-cell
        # variant and wiring its RESET_B / SET_B to the original control net.
        if ff.async_kind == "reset":
            cell_type = YOSYS_SCAN_RESET_CELL_TYPE
            port_directions["RESET_B"] = "input"
            connections["RESET_B"] = [ff.async_net]
        elif ff.async_kind == "set":
            cell_type = YOSYS_SCAN_SET_CELL_TYPE
            port_directions["SET_B"] = "input"
            connections["SET_B"] = [ff.async_net]
        else:
            cell_type = YOSYS_SCAN_CELL_TYPE
        cells[record.instance] = {
            "hide_name": ff.cell.get("hide_name", 0),
            "type": cell_type,
            "parameters": dict(ff.cell.get("parameters", {})),
            "attributes": attrs,
            "port_directions": port_directions,
            "connections": connections,
        }

    for chain in plan.chains:
        _add_port(stitched_module, chain.scan_out, "output", chain.scan_out_net)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(stitched, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ScanStitchResult(
        top=plan.top,
        output_json=output_json,
        chain_count=plan.chain_count,
        cell_count=plan.cell_count,
        clock_nets=plan.clock_nets,
        scan_inputs=plan.scan_inputs,
        scan_outputs=plan.scan_outputs,
        scan_enable=plan.scan_enable,
        chains=plan.chains,
        cells=plan.cells,
        ineligible_ffs=plan.ineligible_ffs,
        wrapper_chains=wrapper_chains,
    )
