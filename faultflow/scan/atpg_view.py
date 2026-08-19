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
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.stitch import (
    SCAN_CELL_TYPES,
    YOSYS_CAPTURE_AND_CELL,
    YOSYS_CAPTURE_INV_CELL,
    YOSYS_CAPTURE_OR_CELL,
    _load_json,
    _top_module,
)

PPI_PREFIX = "__ppi_"
PPO_PREFIX = "__ppo_"
OBSERVE_BUF_CELL = "$faultflow_observe_buf"
D_BRANCH_BUF_CELL = "$faultflow_d_branch_buf"
# Bare (unescaped) spelling of stitch.py's YOSYS_CAPTURE_*_CELL constants --
# derived, not re-typed, so the two producers of these cell types can't drift.
CAPTURE_AND_CELL = YOSYS_CAPTURE_AND_CELL.lstrip("\\")
CAPTURE_OR_CELL = YOSYS_CAPTURE_OR_CELL.lstrip("\\")
CAPTURE_INV_CELL = YOSYS_CAPTURE_INV_CELL.lstrip("\\")
DATA_PIN = "D"
# Generic scan-cell types carry their async control on these pins (active-low):
#   reset variant ($scanff_r): RESET_B, captured value 0 when active.
#   set   variant ($scanff_s): SET_B,   captured value 1 when active.
SCAN_RESET_TYPES = frozenset({"$scanff_r_faultflow", "\\$scanff_r_faultflow"})
SCAN_SET_TYPES = frozenset({"$scanff_s_faultflow", "\\$scanff_s_faultflow"})
ATPG_VIEW_SCHEMA_VER = "scan-atpg-view-observe-buf-1"


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
    clock_nets: list[int],
) -> None:
    used = _nets_used_by_cells(module)
    scan_names = _manifest_scan_port_names(manifest)
    for clk_net in clock_nets:
        clock_name = _clock_port_name(module, clk_net)
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


def _current_data_net(cell: dict[str, Any], fallback: int) -> int:
    """The FF's D-pin net as it is *currently* wired.

    FFs are processed in instance-sort order, and each rewires its own Q net to a
    PPI bit everywhere in the module. When FF A's Q directly drives FF B's D (a
    shift-register / pipeline stage) and A is processed before B, A's rewrite has
    already replaced B's D pin with A's PPI net by the time B is processed. The
    manifest's `data_net` is the *pre-rewire* value and is now stale, so the
    observe buffer must be built from the cell's live D connection instead.
    Falls back to the manifest value if the D pin is not a single integer net
    (e.g. tied to a constant).
    """
    conns = cell.get("connections", {})
    if not isinstance(conns, dict):
        return fallback
    bits = _all_int_bits(conns.get(DATA_PIN, []))
    return bits[0] if len(bits) == 1 else fallback


def _bit_to_single_net_name(module: dict[str, Any]) -> dict[int, str]:
    """Map each single-bit net's bit id -> its net name (first name wins).

    Built once per module so resolving a net name from a bit id is an O(1) dict
    lookup instead of an O(netnames) linear scan. Called per scan FF, the old
    linear scan made view construction O(FFs * netnames) -- ~45 min on a
    12k-FF / 148k-net design; this makes it O(netnames + FFs).
    """
    result: dict[int, str] = {}
    netnames = module.get("netnames", {})
    if not isinstance(netnames, dict):
        return result
    for name, net in netnames.items():
        if not isinstance(net, dict):
            continue
        bits = _all_int_bits(net.get("bits"))
        if len(bits) == 1 and bits[0] not in result:
            result[bits[0]] = str(name)
    return result


class _BitIndex:
    """Incremental ``bit -> location`` index over a module's cells/ports/nets.

    Resolving "where does net bit B appear" and rewiring a net used to be a full
    scan of every cell (plus ports and nets) -- run once per scan FF, i.e.
    O(FFs * netlist_size), which spins for tens of minutes on a 12k-FF design.
    This index answers a consumer count in O(1) and rewires a net in
    O(occurrences of that net), and is kept in sync as the view builder rewires
    nets and inserts mux/buf cells. Behaviour matches the old per-scan rewrite
    for every location that actually holds the rewired bit.
    """

    def __init__(self, module: dict[str, Any]) -> None:
        cells = module.get("cells", {})
        ports = module.get("ports", {})
        netnames = module.get("netnames", {})
        self._cells: dict[str, Any] = cells if isinstance(cells, dict) else {}
        self._ports: dict[str, Any] = ports if isinstance(ports, dict) else {}
        self._netnames: dict[str, Any] = netnames if isinstance(netnames, dict) else {}
        self._cell_locs: dict[int, set[tuple[str, str]]] = {}
        self._port_locs: dict[int, set[str]] = {}
        self._net_locs: dict[int, set[str]] = {}
        for instance, cell in self._cells.items():
            self.add_cell(str(instance), cell)
        for name, port in self._ports.items():
            if isinstance(port, dict):
                for bit in _all_int_bits(port.get("bits")):
                    self._port_locs.setdefault(bit, set()).add(str(name))
        for name, net in self._netnames.items():
            if isinstance(net, dict):
                for bit in _all_int_bits(net.get("bits")):
                    self._net_locs.setdefault(bit, set()).add(str(name))

    def add_cell(self, instance: str, cell: dict[str, Any]) -> None:
        """Index a cell's connections (call for every cell inserted mid-build)."""
        if not isinstance(cell, dict):
            return
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            return
        for pin, bits in conns.items():
            for bit in _all_int_bits(bits):
                self._cell_locs.setdefault(bit, set()).add((instance, str(pin)))

    def remove_cell(self, instance: str, cell: dict[str, Any]) -> None:
        """Drop a cell's connections from the index (call when it is removed).

        A scan FF cell is popped from the reduced view after its Q net is rewired;
        without this, its stale (instance, pin) sites would inflate later consumer
        counts (the old full-cell scan simply never saw the removed cell).
        """
        if not isinstance(cell, dict):
            return
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            return
        for pin, bits in conns.items():
            for bit in _all_int_bits(bits):
                locs = self._cell_locs.get(bit)
                if locs is not None:
                    locs.discard((instance, str(pin)))

    def consumer_count(self, bit: int) -> int:
        """Number of distinct (cell, pin) sites that read/write ``bit``."""
        return len(self._cell_locs.get(bit, ()))

    def rewire(self, old_bit: int, new_bit: int, excluded_ports: set[str]) -> None:
        """Replace ``old_bit`` with ``new_bit`` everywhere it is wired."""
        if old_bit == new_bit:
            return
        for instance, pin in self._cell_locs.pop(old_bit, set()):
            cell = self._cells.get(instance)
            if not isinstance(cell, dict):
                continue
            conns = cell.get("connections", {})
            if not isinstance(conns, dict) or pin not in conns:
                continue
            raw_bits = _all_int_bits(conns[pin])
            conns[pin] = [new_bit if bit == old_bit else bit for bit in raw_bits]
            self._cell_locs.setdefault(new_bit, set()).add((instance, pin))
        moved_ports: set[str] = set()
        for name in self._port_locs.pop(old_bit, set()):
            if name in excluded_ports:
                self._port_locs.setdefault(old_bit, set()).add(name)
                continue
            port = self._ports.get(name)
            if isinstance(port, dict) and isinstance(port.get("bits"), list):
                port["bits"] = [
                    new_bit if bit == old_bit else bit for bit in port["bits"]
                ]
                moved_ports.add(name)
        if moved_ports:
            self._port_locs.setdefault(new_bit, set()).update(moved_ports)
        moved_nets: set[str] = set()
        for name in self._net_locs.pop(old_bit, set()):
            if name in excluded_ports:
                self._net_locs.setdefault(old_bit, set()).add(name)
                continue
            net = self._netnames.get(name)
            if isinstance(net, dict) and isinstance(net.get("bits"), list):
                net["bits"] = [
                    new_bit if bit == old_bit else bit for bit in net["bits"]
                ]
                moved_nets.add(name)
        if moved_nets:
            self._net_locs.setdefault(new_bit, set()).update(moved_nets)


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
    index: "_BitIndex | None" = None,
) -> None:
    cells[instance] = {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [in_bit], "Y": [out_bit]},
    }
    if index is not None:
        index.add_cell(instance, cells[instance])


def _add_internal_gate2_cell(
    cells: dict[str, Any],
    instance: str,
    cell_type: str,
    a_bit: int,
    b_bit: int,
    out_bit: int,
    index: "_BitIndex | None" = None,
) -> None:
    cells[instance] = {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "B": "input", "Y": "output"},
        "connections": {"A": [a_bit], "B": [b_bit], "Y": [out_bit]},
    }
    if index is not None:
        index.add_cell(instance, cells[instance])


def _async_capture_control(cell: dict[str, Any]) -> tuple[str, str, int] | None:
    """The scan FF's async control as (kind, pin, control_net), or None.

    kind is "reset" (RESET_B, active-low, captured value 0) or "set"
    (SET_B, active-low, captured value 1).  The control net is read from the
    live cell connection so a prior FF's Q->PPI rewrite does not affect it.
    """
    cell_type = cell.get("type")
    conns = cell.get("connections", {})
    if not isinstance(conns, dict):
        return None
    if cell_type in SCAN_RESET_TYPES:
        bits = _all_int_bits(conns.get("RESET_B"))
        return ("reset", "RESET_B", bits[0]) if bits else None
    if cell_type in SCAN_SET_TYPES:
        bits = _all_int_bits(conns.get("SET_B"))
        return ("set", "SET_B", bits[0]) if bits else None
    return None


def _add_ctrl_branch(
    cells: dict[str, Any],
    *,
    instance: str,
    control: tuple[str, str, int],
    next_id: int,
    index: "_BitIndex | None" = None,
) -> tuple[int, str, int]:
    """Fan the FF's async control through a dedicated branch buffer.

    Returns (control_branch_net, control_boundary_site_key, next_id).  Both the
    output mux (FF Q) and the capture mux (FF D) read this one branch net, so a
    fault on it models the generic netlist's control-pin branch fault (consumer
    = the scan FF, pin RESET_B/SET_B; the FF cell is removed from the reduced
    view).  Mirrors the D-observe boundary's dedicated-net mechanism.
    """
    _kind, pin, control_net = control
    control_branch_net = next_id
    next_id += 1
    _add_internal_buf_cell(
        cells,
        f"$ffctrlbranch_{instance}",
        D_BRANCH_BUF_CELL,
        control_net,
        control_branch_net,
        index,
    )
    control_site_key = canonical_site_key(
        SiteProvenance(
            yosys_net_id=control_net,
            kind="branch",
            consumer_instance=instance,
            input_pin=pin,
        )
    )
    return control_branch_net, control_site_key, next_id


def _ctrl_mux(
    cells: dict[str, Any],
    *,
    instance: str,
    suffix: str,
    data_in: int,
    kind: str,
    control_branch_net: int,
    next_id: int,
    index: "_BitIndex | None" = None,
) -> tuple[int, int]:
    """Model ``control_active ? control_value : data_in`` (active-low control).

    Returns (mux_out_net, next_id).  Used on both the FF output path
    (data_in = PPI) and the FF capture path (data_in = D observe), so the
    async control sits in the combinational cone of a scan-observable point and
    a control-line fault is detectable by implication.  ``suffix`` keeps the
    inserted cell names unique between the two paths.
    """
    if kind == "reset":
        # out = data_in AND RESET_B  (RESET_B=0 forces 0, else passes data_in).
        mux_out = next_id
        next_id += 1
        _add_internal_gate2_cell(
            cells,
            f"$ff{suffix}rstmux_{instance}",
            CAPTURE_AND_CELL,
            data_in,
            control_branch_net,
            mux_out,
            index,
        )
        return mux_out, next_id
    # out = data_in OR NOT(SET_B)  (SET_B=0 forces 1, else passes data_in).
    inv_out = next_id
    next_id += 1
    _add_internal_buf_cell(
        cells,
        f"$ff{suffix}setinv_{instance}",
        CAPTURE_INV_CELL,
        control_branch_net,
        inv_out,
        index,
    )
    mux_out = next_id
    next_id += 1
    _add_internal_gate2_cell(
        cells,
        f"$ff{suffix}setmux_{instance}",
        CAPTURE_OR_CELL,
        data_in,
        inv_out,
        mux_out,
        index,
    )
    return mux_out, next_id


def _resolve_d_observe_boundary(
    cells: dict[str, Any],
    *,
    instance: str,
    d_net: int,
    next_id: int,
    index: "_BitIndex | None" = None,
) -> tuple[int, str, int, int]:
    """Return (observe_input_net, d_boundary_site_key, next_id, d_observe_net_id)."""
    consumer_count = (
        index.consumer_count(d_net)
        if index is not None
        else len(_consumers_of_net(cells, d_net))
    )
    observe_input = d_net
    d_boundary_site_key = stem_site_key(d_net)
    d_observe_net_id = d_net

    if consumer_count > 1:
        d_branch_bit = next_id
        next_id += 1
        branch_instance = f"$ffbranch_{instance}"
        _add_internal_buf_cell(
            cells,
            branch_instance,
            D_BRANCH_BUF_CELL,
            d_net,
            d_branch_bit,
            index,
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
    generic_json: dict[str, Any],
    manifest: dict[str, Any],
    *,
    active_clock_net: int | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return (reduced Yosys JSON, pseudo_port_map keyed by FF instance).

    When active_clock_net is given, only FFs clocked by that net get a PPO
    observe buffer (per-domain transition view).  All FFs still get a PPI so
    inactive-domain FF state can be controlled as held inputs.
    """
    top = str(manifest["top"])
    _, source_module = _top_module(generic_json, top)
    view = copy.deepcopy(generic_json)
    _, module = _top_module(view, top)

    records = _scan_cell_records(manifest)
    if not records:
        # A wrapper-only EXTEST graybox has NO internal scan FFs to reduce to
        # pseudo-ports; its IEEE-1500 WBR cells are handled downstream by
        # fuse_wbr_into_view. Return the netlist unchanged (no pseudo-ports).
        return view, {}

    instances = [str(record["instance"]) for record in records]
    _check_pseudo_names_free(module, instances)

    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    pseudo_port_map: dict[str, dict[str, Any]] = {}
    next_id = _next_net_id(module)
    scan_port_names = _manifest_scan_port_names(manifest)
    # Precompute bit -> net-name once (O(netnames)); the per-FF lookups below are
    # then O(1) instead of a linear scan over every net for every scan FF.
    net_name_by_bit = _bit_to_single_net_name(source_module)
    # Incremental bit -> location index so per-FF net rewiring and consumer counts
    # touch only each net's actual sites, not a full-module scan every iteration.
    bit_index = _BitIndex(module)

    for record in records:
        instance = str(record["instance"])
        cell = cells.get(instance)
        if not isinstance(cell, dict):
            raise ScanError(f"scan cell missing from generic JSON: {instance}")
        if cell.get("type") not in SCAN_CELL_TYPES:
            raise ScanError(f"{instance}: expected scan FF cell type")

        q_net = int(record["q_net"])
        record_clock_net = int(record.get("clock_net", -1))
        # An async control forces the FF asynchronously, so it overrides BOTH the
        # FF output (current state, drives logic + POs) and the captured value.
        # Model it on both paths so the reduced view matches the full protocol.
        control = _async_capture_control(cell)
        control_branch_net: int | None = None
        control_site_key: str | None = None
        if control is not None:
            control_branch_net, control_site_key, next_id = _add_ctrl_branch(
                cells,
                instance=instance,
                control=control,
                next_id=next_id,
                index=bit_index,
            )
        ppi_port = _ppi_name(instance)
        ppi_bit = next_id
        next_id += 1

        _add_port(module, ppi_port, "input", ppi_bit)
        # FF output seen by downstream logic = control_active ? value : PPI.
        q_drive = ppi_bit
        if control is not None and control_branch_net is not None:
            q_drive, next_id = _ctrl_mux(
                cells,
                instance=instance,
                suffix="q",
                data_in=ppi_bit,
                kind=control[0],
                control_branch_net=control_branch_net,
                next_id=next_id,
                index=bit_index,
            )
        bit_index.rewire(q_net, q_drive, scan_port_names)

        # Read D from the live cell connection, AFTER this record's own Q->PPI
        # rewrite above: a prior FF's rewrite may already have rewired this D pin
        # (FF-to-FF stage), but a self-referencing FF -- D wired directly to its
        # own Q (e.g. stitch.py's constant-tied-enable-inactive "permanent hold"
        # case: no mux, D == Q) -- has its OWN just-completed rewrite invalidate
        # d_net within this same iteration. Reading it only once, before that
        # rewrite, would tap the stale (soon-orphaned) net instead of the PPI.
        d_net = _current_data_net(cell, int(record["data_net"]))

        # Create PPO only for FFs in the active domain (or always when single-domain).
        ppo_is_active = active_clock_net is None or record_clock_net == active_clock_net
        ppo_port: str | None
        ppo_bit: int | None
        boundary: dict[str, Any]
        if ppo_is_active:
            _ppo_port_str = _ppo_name(instance)
            _ppo_bit_int = next_id
            next_id += 1

            _add_port(module, _ppo_port_str, "output", _ppo_bit_int)
            observe_input, d_boundary_site_key, next_id, d_observe_net_id = (
                _resolve_d_observe_boundary(
                    cells,
                    instance=instance,
                    d_net=d_net,
                    next_id=next_id,
                    index=bit_index,
                )
            )
            # Model the async control on the capture path too, so the PPO (the
            # unloaded captured value) reflects `control_active ? value : D`.
            # Reuses the FF's one control branch net, so a control-line fault
            # propagates to a scan-observable PPO (implication-based detection).
            if control is not None and control_branch_net is not None:
                observe_input, next_id = _ctrl_mux(
                    cells,
                    instance=instance,
                    suffix="d",
                    data_in=observe_input,
                    kind=control[0],
                    control_branch_net=control_branch_net,
                    next_id=next_id,
                    index=bit_index,
                )
            _add_internal_buf_cell(
                cells,
                f"$ffobserve_{instance}",
                OBSERVE_BUF_CELL,
                observe_input,
                _ppo_bit_int,
                bit_index,
            )
            ppo_port = _ppo_port_str
            ppo_bit = _ppo_bit_int
            boundary = {
                "atpg_view_schema_ver": ATPG_VIEW_SCHEMA_VER,
                "d_boundary_site_key": d_boundary_site_key,
                "d_observe_net_id": d_observe_net_id,
                "q_stem_site_key": stem_site_key(q_net),
                "unload_capable": True,
            }
        else:
            ppo_port = None
            ppo_bit = None
            boundary = {}
        # The control branch fault maps to its dedicated branch net whether or
        # not this FF has a PPO: the output mux always feeds downstream logic
        # (which may reach an active observable), so record it on either path.
        if control_site_key is not None and control_branch_net is not None:
            boundary["control_boundary_site_key"] = control_site_key
            boundary["control_observe_net_id"] = control_branch_net

        bit_index.remove_cell(instance, cell)
        cells.pop(instance, None)
        pseudo_port_map[instance] = {
            "ppi_port": ppi_port,
            "ppo_port": ppo_port,
            "ppi_net_id": ppi_bit,
            "ppo_net_id": ppo_bit,
            "original_q_net": net_name_by_bit.get(q_net) or str(q_net),
            "original_d_net": net_name_by_bit.get(d_net) or str(d_net),
            "chain_id": int(record["chain_index"]),
            "position_in_chain": int(record["chain_position"]),
            "clock_net": record_clock_net,
            "boundary": boundary,
        }

    clock_nets_list = manifest_clock_net_ids(manifest)
    _drop_dangling_scan_ports(module, manifest, clock_nets_list)

    attrs = module.setdefault("attributes", {})
    if isinstance(attrs, dict):
        attrs["faultflow_atpg_view_schema_ver"] = ATPG_VIEW_SCHEMA_VER

    return view, pseudo_port_map


def build_scan_atpg_view_from_paths(
    generic_json_path: Path | str,
    manifest: dict[str, Any],
    *,
    active_clock_net: int | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    return build_scan_atpg_view(
        _load_json(Path(generic_json_path)),
        manifest,
        active_clock_net=active_clock_net,
    )
