from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from faultflow.coverage.site_key import (
    SiteProvenance,
    canonical_site_key,
    stem_site_key,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.stitch import YOSYS_SCAN_CELL_TYPE, _load_json, _top_module

PPI_PREFIX = "__ppi_"
PPO_PREFIX = "__ppo_"
OBSERVE_BUF_CELL = "$faultflow_observe_buf"
D_BRANCH_BUF_CELL = "$faultflow_d_branch_buf"
DATA_PIN = "D"
ATPG_VIEW_SCHEMA_VER = "v4-observe-buf-1"


def _ppi_name(instance: str) -> str:
    return f"{PPI_PREFIX}{instance}"


def _ppo_name(instance: str) -> str:
    return f"{PPO_PREFIX}{instance}"


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


def _check_pseudo_names_free(module: dict[str, Any], instances: list[str]) -> None:
    existing = _name_set(module)
    names = [_ppi_name(instance) for instance in instances] + [
        _ppo_name(instance) for instance in instances
    ]
    for name in names:
        if name in existing:
            raise ScanError(f"scan ATPG pseudo-port name already exists: {name}")
    if len(set(names)) != len(names):
        raise ScanError("generated scan ATPG pseudo-port names are not unique")


def _add_port(module: dict[str, Any], name: str, direction: str, bit: int) -> None:
    module.setdefault("ports", {})[name] = {"direction": direction, "bits": [bit]}
    module.setdefault("netnames", {})[name] = {
        "hide_name": 0,
        "bits": [bit],
        "attributes": {},
    }


def _nets_used_by_cells(module: dict[str, Any]) -> set[int]:
    used: set[int] = set()
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return used
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for bits in conns.values():
            used.update(_all_int_bits(bits))
    return used


def _scan_cell_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    cells = manifest.get("cells", [])
    if not isinstance(cells, list):
        raise ScanError("manifest cells must be a list")
    records = [cell for cell in cells if isinstance(cell, dict)]
    return sorted(records, key=lambda row: str(row.get("instance", "")))


def _manifest_scan_port_names(manifest: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    scan_enable = manifest.get("scan_enable")
    if isinstance(scan_enable, str) and scan_enable:
        names.add(scan_enable)
    for key in ("scan_inputs", "scan_outputs"):
        value = manifest.get(key, [])
        if isinstance(value, list):
            names.update(str(name) for name in value)
    return names


def _clock_port_name(module: dict[str, Any], clock_net: int) -> str | None:
    ports = module.get("ports", {})
    if not isinstance(ports, dict):
        return None
    for name, port in ports.items():
        if not isinstance(port, dict):
            continue
        bits = _all_int_bits(port.get("bits"))
        if len(bits) == 1 and bits[0] == clock_net:
            return str(name)
    return None


def _drop_dangling_scan_ports(
    module: dict[str, Any],
    manifest: dict[str, Any],
    clock_net: int,
) -> None:
    used = _nets_used_by_cells(module)
    scan_names = _manifest_scan_port_names(manifest)
    clock_name = _clock_port_name(module, clock_net)
    if clock_name is not None:
        scan_names.add(clock_name)
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})
    if not isinstance(ports, dict) or not isinstance(netnames, dict):
        return
    for name in list(scan_names):
        port = ports.get(name)
        if not isinstance(port, dict):
            continue
        bits = _all_int_bits(port.get("bits"))
        if len(bits) != 1:
            continue
        if bits[0] not in used:
            ports.pop(name, None)
            netnames.pop(name, None)


def _net_name_for_bit(module: dict[str, Any], bit: int) -> str | None:
    netnames = module.get("netnames", {})
    if not isinstance(netnames, dict):
        return None
    for name, net in netnames.items():
        if not isinstance(net, dict):
            continue
        bits = _all_int_bits(net.get("bits"))
        if len(bits) == 1 and bits[0] == bit:
            return str(name)
    return None


def _rewire_net_in_cells(cells: dict[str, Any], old_bit: int, new_bit: int) -> None:
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for pin, bits in conns.items():
            raw_bits = _all_int_bits(bits)
            if not raw_bits:
                continue
            conns[pin] = [new_bit if bit == old_bit else bit for bit in raw_bits]


def _consumers_of_net(cells: dict[str, Any], net_bit: int) -> list[tuple[str, str]]:
    consumers: list[tuple[str, str]] = []
    for instance, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for pin, bits in conns.items():
            if net_bit in _all_int_bits(bits):
                consumers.append((str(instance), str(pin)))
    return consumers


def _add_internal_buf_cell(
    cells: dict[str, Any],
    instance: str,
    cell_type: str,
    in_bit: int,
    out_bit: int,
) -> None:
    cells[instance] = {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [in_bit], "Y": [out_bit]},
    }


def _resolve_d_observe_boundary(
    cells: dict[str, Any],
    *,
    instance: str,
    d_net: int,
    next_id: int,
) -> tuple[int, str, int, int]:
    """Return (observe_input_net, d_boundary_site_key, next_id, d_observe_net_id)."""
    consumers = _consumers_of_net(cells, d_net)
    observe_input = d_net
    d_boundary_site_key = stem_site_key(d_net)
    d_observe_net_id = d_net

    if len(consumers) > 1:
        d_branch_bit = next_id
        next_id += 1
        branch_instance = f"$ffbranch_{instance}"
        _add_internal_buf_cell(
            cells,
            branch_instance,
            D_BRANCH_BUF_CELL,
            d_net,
            d_branch_bit,
        )
        observe_input = d_branch_bit
        d_observe_net_id = d_branch_bit
        d_boundary_site_key = canonical_site_key(
            SiteProvenance(
                yosys_net_id=d_net,
                kind="branch",
                consumer_instance=instance,
                input_pin=DATA_PIN,
            )
        )

    return observe_input, d_boundary_site_key, next_id, d_observe_net_id


def build_scan_atpg_view(
    generic_json: dict[str, Any], manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return (reduced Yosys JSON, pseudo_port_map keyed by FF instance)."""
    top = str(manifest["top"])
    _, source_module = _top_module(generic_json, top)
    view = copy.deepcopy(generic_json)
    _, module = _top_module(view, top)

    records = _scan_cell_records(manifest)
    if not records:
        raise ScanError("manifest has no scan cells for ATPG view")

    instances = [str(record["instance"]) for record in records]
    _check_pseudo_names_free(module, instances)

    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    pseudo_port_map: dict[str, dict[str, Any]] = {}
    next_id = _next_net_id(module)

    for record in records:
        instance = str(record["instance"])
        cell = cells.get(instance)
        if not isinstance(cell, dict):
            raise ScanError(f"scan cell missing from generic JSON: {instance}")
        if cell.get("type") not in {YOSYS_SCAN_CELL_TYPE, "$scanff_faultflow"}:
            raise ScanError(f"{instance}: expected scan FF cell type")

        q_net = int(record["q_net"])
        d_net = int(record["data_net"])
        ppi_port = _ppi_name(instance)
        ppo_port = _ppo_name(instance)
        ppi_bit = next_id
        next_id += 1
        ppo_bit = next_id
        next_id += 1

        _add_port(module, ppi_port, "input", ppi_bit)
        _add_port(module, ppo_port, "output", ppo_bit)
        _rewire_net_in_cells(cells, q_net, ppi_bit)

        observe_input, d_boundary_site_key, next_id, d_observe_net_id = (
            _resolve_d_observe_boundary(
                cells,
                instance=instance,
                d_net=d_net,
                next_id=next_id,
            )
        )
        _add_internal_buf_cell(
            cells,
            f"$ffobserve_{instance}",
            OBSERVE_BUF_CELL,
            observe_input,
            ppo_bit,
        )

        cells.pop(instance, None)
        pseudo_port_map[instance] = {
            "ppi_port": ppi_port,
            "ppo_port": ppo_port,
            "original_q_net": _net_name_for_bit(source_module, q_net) or str(q_net),
            "original_d_net": _net_name_for_bit(source_module, d_net) or str(d_net),
            "chain_id": int(record["chain_index"]),
            "position_in_chain": int(record["chain_position"]),
            "boundary": {
                "atpg_view_schema_ver": ATPG_VIEW_SCHEMA_VER,
                "d_boundary_site_key": d_boundary_site_key,
                "d_observe_net_id": d_observe_net_id,
                "q_stem_site_key": stem_site_key(q_net),
                "unload_capable": True,
            },
        }

    clock_net_raw = manifest.get("clock_net")
    if not isinstance(clock_net_raw, int):
        raise ScanError("manifest clock_net must be an integer")
    _drop_dangling_scan_ports(module, manifest, clock_net_raw)

    attrs = module.setdefault("attributes", {})
    if isinstance(attrs, dict):
        attrs["faultflow_atpg_view_schema_ver"] = ATPG_VIEW_SCHEMA_VER

    return view, pseudo_port_map


def build_scan_atpg_view_from_paths(
    generic_json_path: Path | str, manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    return build_scan_atpg_view(_load_json(Path(generic_json_path)), manifest)
