from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError
from faultflow.scan.reports import hash_file
from faultflow.scan.stitch import SCAN_CELL_TYPES, _load_json, _top_module
from faultflow.scan.wbr_view import _WBR_SCAN_IN_TYPES, _WBR_SCAN_OUT_TYPES

# Native shiftable IEEE-1500 WBR scan cells form a wrapper chain threaded through
# CTI (chain in) -> CTO (chain out), the wrapper analogue of an internal scan
# FF's SDI -> Q. A hierarchical EXTEST graybox has ONLY these (no $scanff_*),
# so the structural check must validate them too, not just internal chains.
_WBR_SCAN_CELL_TYPES = _WBR_SCAN_IN_TYPES | _WBR_SCAN_OUT_TYPES


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
    cells_obj: dict[str, Any],
    cell_types: frozenset[str] | set[str],
    in_pin: str,
    out_pin: str,
    require_se: int | None,
    errors: list[str],
) -> tuple[dict[int, str], dict[str, int], set[int], set[str], set[int]]:
    """Index cells of `cell_types` for chain-connectivity validation.

    Returns (node_by_in, out_by_node, clock_nets, names, se_nets): `node_by_in`
    maps the net feeding a cell's chain-input pin (`in_pin`: SDI for internal FFs,
    CTI for wrapper cells) to the cell; `out_by_node` maps a cell to the net on
    its chain-output pin (`out_pin`: Q / CTO). Clock and SE nets are unioned. When
    `require_se` is set (internal chains, whose enable is the manifest
    `scan_enable`), each cell's SE must equal it; wrapper cells pass `None` because
    their enable (`wbr_se`) is a distinct net checked for self-consistency by the
    caller.
    """
    node_by_in: dict[int, str] = {}
    out_by_node: dict[str, int] = {}
    clock_nets: set[int] = set()
    se_nets: set[int] = set()
    names: set[str] = set()
    for instance, cell in cells_obj.items():
        if not isinstance(cell, dict) or cell.get("type") not in cell_types:
            continue
        instance = str(instance)
        names.add(instance)
        chain_in = _conn_bit(cell, in_pin, instance)
        chain_out = _conn_bit(cell, out_pin, instance)
        se = _conn_bit(cell, "SE", instance)
        clk = _conn_bit(cell, "CLK", instance)
        if chain_in in node_by_in:
            errors.append(f"multiple scan cells share {in_pin} net {chain_in}")
        node_by_in[chain_in] = instance
        out_by_node[instance] = chain_out
        clock_nets.add(clk)
        se_nets.add(se)
        if require_se is not None and se != require_se:
            errors.append(f"{instance}: SE does not use shared scan enable")
    return node_by_in, out_by_node, clock_nets, names, se_nets


def _validate_chains(
    module: dict[str, Any],
    raw_chains: list[Any],
    node_by_in: dict[int, str],
    out_by_node: dict[str, int],
    seen: set[str],
    errors: list[str],
) -> None:
    """Re-derive each manifest chain's cell order from the netlist's chain-in ->
    chain-out connectivity and require it to match the manifest exactly."""
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
        wrapper_chains = manifest.get("wrapper_chains", [])
        if not isinstance(wrapper_chains, list):
            raise ScanError("manifest wrapper_chains must be a list")

        cells_obj = module.get("cells", {})
        if not isinstance(cells_obj, dict):
            raise ScanError("scanned module cells must be an object")

        # Internal scan FFs (SDI->Q) and wrapper WBR cells (CTI->CTO) are both
        # shiftable chain elements; validate each against its manifest list.
        # Internal cells share the manifest scan_enable; WBR cells have their own
        # enable (wbr_se) -- a distinct net -- so we only require WBR cells to be
        # self-consistent, plus (when there are no internal cells, i.e. a wrapper-
        # only graybox) that the manifest scan_enable names that WBR enable.
        ff_in, ff_out, ff_clocks, ff_names, _ff_se = _collect_chain_cells(
            cells_obj, SCAN_CELL_TYPES, "SDI", "Q", se_net, errors
        )
        wbr_in, wbr_out, wbr_clocks, wbr_names, wbr_se = _collect_chain_cells(
            cells_obj, _WBR_SCAN_CELL_TYPES, "CTI", "CTO", None, errors
        )
        if len(wbr_se) > 1:
            errors.append("WBR scan cells do not share one scan enable")
        elif wbr_se and not ff_names and wbr_se != {se_net}:
            errors.append("WBR scan cells: SE does not use shared scan enable")
        if not (ff_clocks | wbr_clocks):
            errors.append("scanned design has no scan clock net")

        seen: set[str] = set()
        _validate_chains(module, chains, ff_in, ff_out, seen, errors)
        wbr_seen: set[str] = set()
        _validate_chains(module, wrapper_chains, wbr_in, wbr_out, wbr_seen, errors)

        missing = ff_names - seen
        extra = seen - ff_names
        if missing:
            errors.append(
                "scan FFs missing from manifest: " + ", ".join(sorted(missing))
            )
        if extra:
            errors.append("manifest lists non-scan FFs: " + ", ".join(sorted(extra)))

        wbr_missing = wbr_names - wbr_seen
        wbr_extra = wbr_seen - wbr_names
        if wbr_missing:
            errors.append(
                "WBR scan cells missing from manifest wrapper_chains: "
                + ", ".join(sorted(wbr_missing))
            )
        if wbr_extra:
            errors.append(
                "manifest wrapper_chains list non-WBR cells: "
                + ", ".join(sorted(wbr_extra))
            )

        if require_techmap:
            sky = manifest.get("sky130_verilog")
            if not isinstance(sky, str) or not sky or not Path(sky).exists():
                errors.append("required Sky130 techmap artifact is missing")
    except (KeyError, ScanError, OSError) as exc:
        errors.append(str(exc))
    return StructuralCheckResult(warnings=warnings, errors=errors)
