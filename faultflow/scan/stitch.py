from __future__ import annotations

import copy
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError

SCAN_CELL_TYPE = "$scanff_faultflow"
YOSYS_SCAN_CELL_TYPE = "\\$scanff_faultflow"
DEFAULT_SCAN_IN = "scan_in_0"
DEFAULT_SCAN_OUT = "scan_out_0"
DEFAULT_SCAN_ENABLE = "scan_en"


@dataclass(frozen=True)
class ScanCellRecord:
    instance: str
    original_type: str
    clock_net: int
    data_net: int
    scan_in_net: int
    scan_enable_net: int
    q_net: int


@dataclass(frozen=True)
class ScanStitchResult:
    top: str
    output_json: Path
    chain_count: int
    cell_count: int
    clock_net: int
    scan_inputs: list[str]
    scan_outputs: list[str]
    scan_enable: str
    cells: list[ScanCellRecord]


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


def _ff_pin(ff_meta: dict[str, Any], key: str, instance: str) -> str:
    value = ff_meta.get(key)
    if not isinstance(value, str) or not value:
        raise ScanError(f"{instance}: FF metadata is missing {key}")
    return value


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


def _ensure_name_free(module: dict[str, Any], name: str) -> None:
    ports = module.setdefault("ports", {})
    netnames = module.setdefault("netnames", {})
    if name in ports or name in netnames:
        raise ScanError(f"scan artifact name already exists: {name}")


def _add_port(module: dict[str, Any], name: str, direction: str, bit: int) -> None:
    _ensure_name_free(module, name)
    module["ports"][name] = {"direction": direction, "bits": [bit]}
    module["netnames"][name] = {"hide_name": 0, "bits": [bit], "attributes": {}}


def _collect_plain_ffs(
    module: dict[str, Any], cell_map: dict[str, Any]
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for instance, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            raise ScanError(f"{instance}: cell entry is malformed")
        cell_type = str(cell.get("type", ""))
        if cell_type in {SCAN_CELL_TYPE, YOSYS_SCAN_CELL_TYPE}:
            raise ScanError(f"{instance}: design already contains {SCAN_CELL_TYPE}")
        match = _lookup_cell(cell_map, cell_type)
        if match is None:
            continue
        _, entry = match
        if entry.get("node_type") != "FF":
            continue
        ff_meta = entry.get("ff")
        if not isinstance(ff_meta, dict):
            raise ScanError(f"{instance}: FF cell map entry is missing ff metadata")
        if "scan" in ff_meta:
            raise ScanError(
                f"{instance}: scan FF sources are not stitched in this slice"
            )
        if any(key in ff_meta for key in ("clear", "preset", "reset")):
            raise ScanError(f"{instance}: reset/set FF scan replacement is deferred")
        if ff_meta.get("trigger") != "POSEDGE":
            raise ScanError(
                f"{instance}: only posedge FF scan replacement is supported"
            )
        out.append((str(instance), cell, ff_meta))
    if not out:
        raise ScanError("no plain posedge FF cells found to stitch")
    return out


def stitch_scan_json(
    netlist_json: Path,
    cell_map_json: Path,
    top: str,
    output_json: Path,
) -> ScanStitchResult:
    data = _load_json(netlist_json)
    cell_map = _load_json(cell_map_json)
    top_name, module = _top_module(data, top)
    stitched = copy.deepcopy(data)
    _, stitched_module = _top_module(stitched, top_name)

    plain_ffs = _collect_plain_ffs(stitched_module, cell_map)
    next_id = _next_net_id(stitched_module)
    scan_in_bit = next_id
    scan_enable_bit = next_id + 1
    _add_port(stitched_module, DEFAULT_SCAN_IN, "input", scan_in_bit)
    _add_port(stitched_module, DEFAULT_SCAN_ENABLE, "input", scan_enable_bit)

    chain_cells: list[ScanCellRecord] = []
    previous_q = scan_in_bit
    clock_net: int | None = None
    cells = stitched_module["cells"]
    for index, (instance, cell, ff_meta) in enumerate(plain_ffs):
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            raise ScanError(f"{instance}: connections must be an object")
        clk_pin = _ff_pin(ff_meta, "clock", instance)
        d_pin = _ff_pin(ff_meta, "data", instance)
        q_pin = _ff_pin(ff_meta, "output", instance)
        clk = _one_bit(conns.get(clk_pin), f"{instance}.{clk_pin}")
        d_net = _one_bit(conns.get(d_pin), f"{instance}.{d_pin}")
        q_net = _one_bit(conns.get(q_pin), f"{instance}.{q_pin}")
        if clock_net is None:
            clock_net = clk
        elif clock_net != clk:
            raise ScanError("scan stitching supports exactly one clock net")

        original_type = str(cell.get("type", ""))
        attrs = dict(cell.get("attributes", {}))
        attrs.update(
            {
                "faultflow_original_type": original_type,
                "faultflow_scan": "1",
                "faultflow_scan_chain": "0",
                "faultflow_scan_index": str(index),
            }
        )
        cells[instance] = {
            "hide_name": cell.get("hide_name", 0),
            "type": YOSYS_SCAN_CELL_TYPE,
            "parameters": dict(cell.get("parameters", {})),
            "attributes": attrs,
            "port_directions": {
                "CLK": "input",
                "D": "input",
                "SI": "input",
                "SE": "input",
                "Q": "output",
            },
            "connections": {
                "CLK": [clk],
                "D": [d_net],
                "SI": [previous_q],
                "SE": [scan_enable_bit],
                "Q": [q_net],
            },
        }
        chain_cells.append(
            ScanCellRecord(
                instance=instance,
                original_type=original_type,
                clock_net=clk,
                data_net=d_net,
                scan_in_net=previous_q,
                scan_enable_net=scan_enable_bit,
                q_net=q_net,
            )
        )
        previous_q = q_net

    if clock_net is None:
        raise ScanError("internal error: scan stitching found no clock")
    _add_port(stitched_module, DEFAULT_SCAN_OUT, "output", previous_q)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(stitched, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ScanStitchResult(
        top=top_name,
        output_json=output_json,
        chain_count=1,
        cell_count=len(chain_cells),
        clock_net=clock_net,
        scan_inputs=[DEFAULT_SCAN_IN],
        scan_outputs=[DEFAULT_SCAN_OUT],
        scan_enable=DEFAULT_SCAN_ENABLE,
        cells=chain_cells,
    )
