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
        scan_cells: dict[str, dict[str, Any]] = {
            str(name): cell
            for name, cell in cells_obj.items()
            if isinstance(cell, dict) and cell.get("type") in SCAN_CELL_TYPES
        }
        node_by_sdi: dict[int, str] = {}
        q_by_node: dict[str, int] = {}
        clock_nets: set[int] = set()
        for instance, cell in scan_cells.items():
            sdi = _conn_bit(cell, "SDI", instance)
            q = _conn_bit(cell, "Q", instance)
            se = _conn_bit(cell, "SE", instance)
            clk = _conn_bit(cell, "CLK", instance)
            if sdi in node_by_sdi:
                errors.append(f"multiple scan FFs share SDI net {sdi}")
            node_by_sdi[sdi] = instance
            q_by_node[instance] = q
            clock_nets.add(clk)
            if se != se_net:
                errors.append(f"{instance}: SE does not use shared scan enable")
        if len(clock_nets) == 0:
            errors.append("scanned design has no scan clock net")

        seen: set[str] = set()
        for raw_chain in chains:
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
            while cur in node_by_sdi:
                instance = node_by_sdi[cur]
                if instance in derived:
                    errors.append(f"cycle found in chain {raw_chain.get('index')}")
                    break
                derived.append(instance)
                cur = q_by_node[instance]
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
                    errors.append(f"duplicate scan FF in chains: {instance}")
                seen.add(instance)

        missing = set(scan_cells) - seen
        extra = seen - set(scan_cells)
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
