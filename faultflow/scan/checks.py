from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError
from faultflow.scan.reports import hash_file
from faultflow.scan.stitch import SCAN_CELL_TYPES, _load_json, _top_module


@dataclass(frozen=True)
class StructuralCheckResult:
    warnings: list[str]
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def _port_bit(module: dict[str, Any], name: str, direction: str) -> int:
    ports = module.get("ports", {})
    if not isinstance(ports, dict) or name not in ports:
        raise ScanError(f"missing scan port: {name}")
    port = ports[name]
    if not isinstance(port, dict) or port.get("direction") != direction:
        raise ScanError(f"scan port {name} must be direction {direction}")
    bits = port.get("bits")
    if not isinstance(bits, list) or len(bits) != 1 or not isinstance(bits[0], int):
        raise ScanError(f"scan port {name} must be one concrete bit")
    return int(bits[0])


def _conn_bit(cell: dict[str, Any], pin: str, instance: str) -> int:
    conns = cell.get("connections", {})
    if not isinstance(conns, dict):
        raise ScanError(f"{instance}: connections must be an object")
    bits = conns.get(pin)
    if not isinstance(bits, list) or len(bits) != 1 or not isinstance(bits[0], int):
        raise ScanError(f"{instance}.{pin} must be one concrete bit")
    return int(bits[0])


def _collect_chain_cells(
    cells_obj: dict[str, Any], se_net: int, errors: list[str]
) -> tuple[dict[int, str], dict[str, int], set[int], set[str]]:
    """Index the scan cells for chain-connectivity validation.

    Returns (node_by_in, out_by_node, clock_nets, names): `node_by_in` maps the
    net feeding a cell's SDI to the cell; `out_by_node` maps a cell to its Q net.
    Each cell's SE must be the manifest's scan enable (`se_net`)."""
    node_by_in: dict[int, str] = {}
    out_by_node: dict[str, int] = {}
    clock_nets: set[int] = set()
    names: set[str] = set()
    for instance, cell in cells_obj.items():
        if not isinstance(cell, dict) or cell.get("type") not in SCAN_CELL_TYPES:
            continue
        instance = str(instance)
        names.add(instance)
        chain_in = _conn_bit(cell, "SDI", instance)
        if chain_in in node_by_in:
            errors.append(f"multiple scan cells share SDI net {chain_in}")
        node_by_in[chain_in] = instance
        out_by_node[instance] = _conn_bit(cell, "Q", instance)
        clock_nets.add(_conn_bit(cell, "CLK", instance))
        if _conn_bit(cell, "SE", instance) != se_net:
            errors.append(f"{instance}: SE does not use shared scan enable")
    return node_by_in, out_by_node, clock_nets, names


def _validate_chains(
    module: dict[str, Any],
    raw_chains: list[Any],
    node_by_in: dict[int, str],
    out_by_node: dict[str, int],
    seen: set[str],
    errors: list[str],
) -> None:
    """Re-derive each manifest chain's cell order from the netlist's SDI -> Q
    connectivity and require it to match the manifest exactly."""
    for raw_chain in raw_chains:
        if not isinstance(raw_chain, dict):
            errors.append("manifest chain entry must be an object")
            continue
        scan_in = str(raw_chain.get("scan_in"))
        scan_out = str(raw_chain.get("scan_out"))
        expected_cells = [str(cell) for cell in raw_chain.get("cells", [])]
        try:
            cur = _port_bit(module, scan_in, "input")
            scan_out_net = _port_bit(module, scan_out, "output")
        except ScanError as exc:
            errors.append(str(exc))
            continue
        derived: list[str] = []
        while cur in node_by_in:
            instance = node_by_in[cur]
            if instance in derived:
                errors.append(f"cycle found in chain {raw_chain.get('index')}")
                break
            derived.append(instance)
            cur = out_by_node[instance]
        if cur != scan_out_net:
            errors.append(
                f"chain {raw_chain.get('index')} terminates at net {cur}, "
                f"not scan_out {scan_out_net}"
            )
        if derived != expected_cells:
            errors.append(
                f"chain {raw_chain.get('index')} manifest order does not "
                "match JSON connectivity"
            )
        for instance in derived:
            if instance in seen:
                errors.append(f"duplicate scan cell in chains: {instance}")
            seen.add(instance)


def check_scan_structure(
    manifest: dict[str, object],
    require_techmap: bool = False,
) -> StructuralCheckResult:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        generic_json = Path(str(manifest["generic_json"]))
        expected_hash = str(manifest.get("generic_json_hash", ""))
        actual_hash = hash_file(generic_json)
        if actual_hash != expected_hash:
            errors.append("generic JSON hash does not match manifest")
        data = _load_json(generic_json)
        top = str(manifest["top"])
        _, module = _top_module(data, top)

        scan_enable = str(manifest["scan_enable"])
        se_net = _port_bit(module, scan_enable, "input")

        chains = manifest.get("chains", [])
        if not isinstance(chains, list):
            raise ScanError("manifest chains must be a list")

        cells_obj = module.get("cells", {})
        if not isinstance(cells_obj, dict):
            raise ScanError("scanned module cells must be an object")

        # Every scan flop -- an IEEE 1500 wrapper's among them, on chains of
        # their own -- shifts SDI -> Q on the manifest's scan enable.
        ff_in, ff_out, ff_clocks, ff_names = _collect_chain_cells(
            cells_obj, se_net, errors
        )
        if not ff_clocks:
            errors.append("scanned design has no scan clock net")

        seen: set[str] = set()
        _validate_chains(module, chains, ff_in, ff_out, seen, errors)

        missing = ff_names - seen
        extra = seen - ff_names
        if missing:
            errors.append(
                "scan FFs missing from manifest: " + ", ".join(sorted(missing))
            )
        if extra:
            errors.append("manifest lists non-scan FFs: " + ", ".join(sorted(extra)))

        if require_techmap:
            sky = manifest.get("sky130_verilog")
            if not isinstance(sky, str) or not sky or not Path(sky).exists():
                errors.append("required Sky130 techmap artifact is missing")
    except (KeyError, ScanError, OSError) as exc:
        errors.append(str(exc))
    return StructuralCheckResult(warnings=warnings, errors=errors)
