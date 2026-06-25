"""INTEST + internal-scan fusion (IEEE 1500).

`build_scan_atpg_view` turns a sequential core's scan flip-flops into a
combinational pseudo-PI/PO view (`__ppi_*` inputs, `__ppo_*` observed outputs).
On its own that view tests the core through the *top-level* ports. For INTEST of
a wrapped core we instead want the **wrapper boundary** to be the control/observe
boundary: each `$wbc_in_faultflow` core net becomes a controllable pseudo-PI and
each `$wbc_out_faultflow` core net becomes an observed pseudo-PO, while the
system-side top ports are decoupled.

`fuse_wbr_into_view` runs as a second pass over the scan-reduced view and rewrites
it so the fused view's PIs = {`__ppi_*` scan FF state} ∪ {`__wbi_*` core inputs}
and POs = {`__ppo_*` scan FF data} ∪ {`__wbo_*` core outputs}. The result is an
ordinary combinational netlist, so the existing scan SAT ATPG / fault sim run
unchanged. The C++ engines and `build_mode_config` are untouched.

This is the boundary-fusion layer only; wiring it through the full `sim --scan`
golden-protocol gate on the generic netlist is a separate step (the generic
netlist still carries the real wrapper cells).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from faultflow.scan.atpg_view import (
    OBSERVE_BUF_CELL,
    PPO_PREFIX,
    _add_internal_buf_cell,
    _add_port,
    _all_int_bits,
    _next_net_id,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.stitch import _top_module

WBI_PREFIX = "__wbi_"
WBO_PREFIX = "__wbo_"
# EXTEST exposes the *system* side of the boundary: wbc_out system nets become
# controllable interconnect drivers (`__wbo_ctl_*`) and wbc_in system nets become
# observed interconnect taps (`__wbi_obs_*`).
WBO_CTL_PREFIX = "__wbo_ctl_"
WBI_OBS_PREFIX = "__wbi_obs_"
WBR_SCAN_VIEW_SCHEMA_VER = "wbr-scan-intest-1"
WBR_SCAN_EXTEST_VIEW_SCHEMA_VER = "wbr-scan-extest-1"
# Yosys JSON constant-0 literal. A BUF whose input reads this drives its output
# net to 0 (the C++ normalizer folds "0" to a CONST0 driver). Used to SAFE the
# decoupled core inputs in EXTEST.
_CONST0_LITERAL = "0"

# IEEE 1500 wrapper boundary cell types (and Yosys backslash-escaped variants).
# Pin names are fixed by the cell definitions in the cell maps.
_WBR_IN_TYPES = {"$wbc_in_faultflow", "\\$wbc_in_faultflow"}
_WBR_OUT_TYPES = {"$wbc_out_faultflow", "\\$wbc_out_faultflow"}
# Native shiftable scan-model WBR cells. They expose the SAME core/sys pins as the
# buffer model (TO_CORE/FROM_SYS, FROM_CORE/TO_SYS) and, in FUNCTIONAL mode, drive
# the core net from the sys net (the mode-mux half lowers to a WBR_IN/WBR_OUT
# buffer in the C++ normalizer) -- so the buffer-model fusion below reduces them
# identically. The only extra: they carry a scan chain (CTI/SE/CTO) whose top
# ports go dangling once the cell is removed and must be dropped.
_WBR_SCAN_IN_TYPES = {"$wbc_in_scan_faultflow", "\\$wbc_in_scan_faultflow"}
_WBR_SCAN_OUT_TYPES = {"$wbc_out_scan_faultflow", "\\$wbc_out_scan_faultflow"}
_WBR_SCAN_CHAIN_PINS = ("CTI", "SE", "CTO")  # NOT CLK (shared with core/main FFs)
_WBR_IN_CORE_PIN = "TO_CORE"
_WBR_IN_SYS_PIN = "FROM_SYS"
_WBR_OUT_CORE_PIN = "FROM_CORE"
_WBR_OUT_SYS_PIN = "TO_SYS"


@dataclass(frozen=True)
class WbrRecord:
    instance: str
    core_net: int
    sys_net: int
    is_input: bool
    # Scan-chain/enable net ids (CTI, SE, CTO) for scan-model cells; empty for
    # buffer-model cells. The fusion pass drops the matching dangling top ports.
    chain_nets: tuple[int, ...] = ()


def _single_bit(cell: dict[str, Any], pin: str, instance: str) -> int:
    conns = cell.get("connections", {})
    bits = _all_int_bits(conns.get(pin)) if isinstance(conns, dict) else []
    if len(bits) != 1:
        raise ScanError(f"wrapper cell {instance}: pin {pin} must be a single net")
    return bits[0]


def _is_constant_bit(cell: dict[str, Any], pin: str) -> bool:
    """True when `pin` is connected to a Yosys constant literal ("0"/"1"/"x"/"z")."""
    conns = cell.get("connections", {})
    if not isinstance(conns, dict):
        return False
    raw = conns.get(pin)
    return (
        isinstance(raw, list)
        and len(raw) == 1
        and isinstance(raw[0], str)
        and raw[0] in {"0", "1", "x", "z"}
    )


def _scan_chain_nets(cell: dict[str, Any]) -> tuple[int, ...]:
    """Net ids on a scan WBR cell's chain/enable pins (CTI, SE, CTO).

    Their top ports (wbr_si / wbr_se / wbr_so) go dangling once the cell is
    removed from the fused view, so the fusion pass drops them. CLK is excluded
    on purpose -- it is shared with the core and the main scan FFs.
    """
    conns = cell.get("connections", {})
    nets: list[int] = []
    if isinstance(conns, dict):
        for pin in _WBR_SCAN_CHAIN_PINS:
            nets.extend(_all_int_bits(conns.get(pin)))
    return tuple(nets)


def extract_wbr_cells(module: dict[str, Any]) -> list[WbrRecord]:
    """All IEEE 1500 boundary cells in `module`, sorted by instance name.

    Matches both the buffer-model (`$wbc_*_faultflow`) and native shiftable
    scan-model (`$wbc_*_scan_faultflow`) cells; scan cells additionally carry
    their dangling chain-port nets in `WbrRecord.chain_nets`.
    """
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")
    records: list[WbrRecord] = []
    for instance, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("type")
        if ctype in _WBR_IN_TYPES or ctype in _WBR_SCAN_IN_TYPES:
            scan = ctype in _WBR_SCAN_IN_TYPES
            # Skip input WBR cells whose TO_CORE is a constant (can't be
            # driven: the system-side has no real net to controllability-test).
            if _is_constant_bit(cell, _WBR_IN_CORE_PIN):
                continue
            records.append(
                WbrRecord(
                    instance=str(instance),
                    core_net=_single_bit(cell, _WBR_IN_CORE_PIN, str(instance)),
                    sys_net=_single_bit(cell, _WBR_IN_SYS_PIN, str(instance)),
                    is_input=True,
                    chain_nets=_scan_chain_nets(cell) if scan else (),
                )
            )
        elif ctype in _WBR_OUT_TYPES or ctype in _WBR_SCAN_OUT_TYPES:
            scan = ctype in _WBR_SCAN_OUT_TYPES
            # Skip output WBR cells whose FROM_CORE is a constant literal
            # ("0"/"1"/"x"): a constant-driven boundary bit is always fixed
            # and cannot be an INTEST observation site. This occurs when
            # Yosys ties off unused output port bits (e.g. mem_addr[0] in
            # picorv32a where the lsb is permanently 0).
            if _is_constant_bit(cell, _WBR_OUT_CORE_PIN):
                continue
            records.append(
                WbrRecord(
                    instance=str(instance),
                    core_net=_single_bit(cell, _WBR_OUT_CORE_PIN, str(instance)),
                    sys_net=_single_bit(cell, _WBR_OUT_SYS_PIN, str(instance)),
                    is_input=False,
                    chain_nets=_scan_chain_nets(cell) if scan else (),
                )
            )
    return sorted(records, key=lambda r: r.instance)


def extract_wbr_chain_bits(module: dict[str, Any]) -> set[int]:
    """CTI/SE/CTO net IDs from ALL WBR scan cells in *module*.

    Unlike extract_wbr_cells, this includes cells skipped for INTEST
    observation (e.g. constant-FROM_CORE WBR out cells). The C++ normalizer
    still lowers those cells and their chain-net fault sites appear in the
    generic site-key listing; callers must exclude them as ``scan_chain``.
    """
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return set()
    bits: set[int] = set()
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("type")
        if ctype in (_WBR_SCAN_IN_TYPES | _WBR_SCAN_OUT_TYPES):
            bits.update(_scan_chain_nets(cell))
    return bits


def _drop_top_ports_by_bit(module: dict[str, Any], bits: set[int]) -> None:
    """Remove single-bit top-level ports whose net is now dangling (decoupled)."""
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})
    if not isinstance(ports, dict):
        return
    for name in list(ports):
        port = ports[name]
        if not isinstance(port, dict):
            continue
        pbits = _all_int_bits(port.get("bits"))
        if len(pbits) == 1 and pbits[0] in bits:
            ports.pop(name, None)
            if isinstance(netnames, dict):
                netnames.pop(name, None)


def _add_const0_driver_cell(cells: dict[str, Any], instance: str, out_bit: int) -> None:
    """Insert a BUF whose input is the constant-0 literal, driving `out_bit` to 0.

    The C++ normalizer folds the Yosys ``"0"`` literal to a CONST0 driver, so the
    BUF passes a hard 0 onto `out_bit`. This re-drives a core-input net that lost
    its wrapper driver in EXTEST, leaving it a real (safe-0) net.
    """
    cells[instance] = {
        "hide_name": 0,
        "type": OBSERVE_BUF_CELL,
        "parameters": {},
        "attributes": {"faultflow_internal": "1"},
        "port_directions": {"A": "input", "Y": "output"},
        "connections": {"A": [_CONST0_LITERAL], "Y": [out_bit]},
    }


def _ppo_bits(module: dict[str, Any]) -> set[int]:
    """Single-bit net ids exposed by every `__ppo_*` output port."""
    bits: set[int] = set()
    ports = module.get("ports", {})
    if not isinstance(ports, dict):
        return bits
    for name, port in ports.items():
        if not str(name).startswith(PPO_PREFIX) or not isinstance(port, dict):
            continue
        pbits = _all_int_bits(port.get("bits"))
        if len(pbits) == 1:
            bits.add(pbits[0])
    return bits


def _drop_ppo_ports_and_observers(module: dict[str, Any]) -> None:
    """Remove every `__ppo_*` output port and the observe buffer that feeds it.

    In EXTEST the core is dead, so its scan-exposed internals must not be
    observable. Each `__ppo_*` port bit is driven by exactly one observe buffer
    (`Y` == port bit); that cell is deleted along with the port.
    """
    ppo_bits = _ppo_bits(module)
    cells = module.get("cells", {})
    if isinstance(cells, dict):
        for inst in list(cells):
            cell = cells[inst]
            if not isinstance(cell, dict):
                continue
            conns = cell.get("connections", {})
            y_bits = _all_int_bits(conns.get("Y")) if isinstance(conns, dict) else []
            if len(y_bits) == 1 and y_bits[0] in ppo_bits:
                cells.pop(inst, None)
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})
    for container in (ports, netnames):
        if not isinstance(container, dict):
            continue
        for name in list(container):
            if str(name).startswith(PPO_PREFIX):
                container.pop(name, None)


def _bit_to_name_index(module: dict[str, Any]) -> dict[int, str]:
    """Build a deterministic int-net-id -> name map for a top module.

    Prefers a top-level port name over a netname entry. Among duplicates (two
    ports / netnames share the same bit) the lexicographically smallest name
    wins so the result is deterministic across JSON key orderings.
    """

    def _lex_smallest_by_bit(container: Any) -> dict[int, str]:
        out: dict[int, str] = {}
        if not isinstance(container, dict):
            return out
        for name, entry in container.items():
            if not isinstance(entry, dict):
                continue
            for bit in _all_int_bits(entry.get("bits")):
                if bit not in out or str(name) < out[bit]:
                    out[bit] = str(name)
        return out

    net_idx = _lex_smallest_by_bit(module.get("netnames", {}))
    port_idx = _lex_smallest_by_bit(module.get("ports", {}))
    # Ports always win over netnames; within each tier the lex-smallest name wins
    # so the result is deterministic across JSON key orderings.
    return {**net_idx, **port_idx}


def build_wbr_generic_name_map(
    generic_json: dict[str, Any],
    top: str,
    wbr_port_map: dict[str, dict[str, Any]],
    mode: str,
    manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str], set[int]]:
    """Map fused INTEST boundary port names back to their generic-netlist names.

    The C++ scan golden gate always simulates the *generic* netlist (which still
    carries the real ``$wbc_*`` transparent-buffer cells) in plain FUNCTIONAL
    mode, so before handing it stimulus / observe keys we must translate the
    fused names (``__wbi_*``, ``__wbo_*``) back to the generic top-port names
    that carry the same physical signal.

    In INTEST this reconciliation is exact because the boundary control/observe
    aligns with port direction:
      * wbc_in  : ``__wbi_<inst>`` drives the core net == (through the transparent
                  buffer) the wbc_in system-side **input** port ``name(sys_net)``.
      * wbc_out : ``__wbo_<inst>`` observes the core net == the wbc_out system-side
                  **output** port ``name(sys_net)``.
    The system-side ``sys_net`` bits are decoupled (their top ports were dropped
    from the fused view) and must be tagged ``wbr_decoupled`` in the generic
    denominator.

    EXTEST is intentionally NOT supported here: it would need to drive a top
    OUTPUT net and observe a top INPUT/internal net, which a direction-respecting
    functional simulation of the generic netlist cannot do (see
    ``Runner._sim_scan``). FUNCTIONAL needs no mapping.

    Returns ``(stimulus_name_by_fused_port, observe_name_by_fused_port,
    decoupled_generic_bits)``.

    Guard: stimulus entries whose resolved generic name is a clock / scan-enable
    / scan-input port (from ``manifest``) are omitted — those pins are driven by
    the scan protocol, not the wrapper. An unresolvable boundary net (no generic
    port or netname) is a hard error: it would otherwise leak a literal fused
    ``__wbi_*``/``__wbo_*`` name into the generic sim as an unknown port.
    """
    if mode == "functional":
        return {}, {}, set()
    if mode != "intest":
        raise ScanError(
            "build_wbr_generic_name_map supports only INTEST golden-gate "
            f"reconciliation, not {mode!r} (see Runner._sim_scan)"
        )

    _, module = _top_module(generic_json, top)
    bit_name = _bit_to_name_index(module)

    # Build the set of clock / scan-enable / scan-input port names we must not
    # drive — those are owned by the scan protocol, not the wrapper boundary.
    clock_scan_ports: set[str] = set()
    if manifest is not None:
        clock_nets_raw = manifest.get("clock_nets")
        if isinstance(clock_nets_raw, list):
            clock_ids = [int(n) for n in clock_nets_raw if isinstance(n, int)]
        else:
            clk = manifest.get("clock_net")
            clock_ids = [int(clk)] if isinstance(clk, int) else []
        for cid in clock_ids:
            if cid in bit_name:
                clock_scan_ports.add(bit_name[cid])
        se = manifest.get("scan_enable")
        if isinstance(se, str):
            clock_scan_ports.add(se)
        for si in manifest.get("scan_inputs", []):
            clock_scan_ports.add(str(si))

    stimulus: dict[str, str] = {}
    observe: dict[str, str] = {}
    decoupled: set[int] = set()

    for instance, entry in wbr_port_map.items():
        fused_port = str(entry.get("port", ""))
        sys_net = int(entry.get("sys_net", -1))
        role = str(entry.get("role", ""))  # "ppi" = input, "ppo" = output

        # sys_net is the generic signal that the fused view decoupled.
        if sys_net >= 0:
            decoupled.add(sys_net)
        generic_name = bit_name.get(sys_net, "")
        if not generic_name:
            raise ScanError(
                f"wrapper boundary {instance}: system net {sys_net} has no port "
                f"or netname in generic top {top!r}; cannot reconcile to the "
                "scan golden gate"
            )
        if role == "ppi":
            # __wbi_<inst> drives what used to be the sys-side input port.
            if generic_name not in clock_scan_ports:
                stimulus[fused_port] = generic_name
        else:
            # __wbo_<inst> observes what used to be the sys-side output port.
            observe[fused_port] = generic_name

    return stimulus, observe, decoupled


def fuse_wbr_into_view(
    view: dict[str, Any], top: str, mode: str = "intest"
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Fuse the wrapper boundary into a scan-reduced view for INTEST/EXTEST.

    Mutates and returns `view`. For each boundary cell, in INTEST mode:
      * `$wbc_in_faultflow`: its core net (`TO_CORE`) becomes a controllable
        pseudo-PI `__wbi_<inst>`; the wrapper cell is removed and the system-side
        top PI feeding `FROM_SYS` is dropped (decoupled).
      * `$wbc_out_faultflow`: its core net (`FROM_CORE`) becomes an observed
        pseudo-PO `__wbo_<inst>` via a `$faultflow_observe_buf`; the wrapper cell
        is removed and the system-side top PO reading `TO_SYS` is dropped.

    In EXTEST mode the roles flip to test the interconnect with a dead core:
      * `$wbc_in_faultflow`: its system net (`FROM_SYS`) is OBSERVED via a
        `$faultflow_observe_buf` -> new output `__wbi_obs_<inst>`; the wrapper
        cell is removed and its core net (`TO_CORE`) is SAFEd to constant 0.
      * `$wbc_out_faultflow`: its system net (`TO_SYS`) becomes a controllable
        interconnect driver -> new input `__wbo_ctl_<inst>`; the wrapper cell is
        removed and its core net (`FROM_CORE`) is left dangling (decoupled).
      * Every `__ppo_*` scan observe port + its observe buffer is dropped (the
        dead core must not be observed); `__ppi_*` inputs are left as-is; the
        top-level PIs/POs are KEPT as EXTEST stimulus/observables.

    Returns (view, wbr_port_map) where wbr_port_map[instance] records the created
    port, its role (`ppi`/`ppo`), and the core/sys nets.
    """
    if mode == "extest":
        return _fuse_wbr_extest(view, top)
    if mode != "intest":
        # FUNCTIONAL (and any unknown mode) needs no boundary fusion.
        return view, {}

    _, module = _top_module(view, top)
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    records = extract_wbr_cells(module)
    next_id = _next_net_id(module)
    wbr_port_map: dict[str, dict[str, Any]] = {}
    sys_nets_to_drop: set[int] = set()

    for rec in records:
        # Scan-model cells leave their chain ports (wbr_si/wbr_se/wbr_so)
        # dangling once removed; drop them alongside the decoupled sys ports.
        sys_nets_to_drop.update(rec.chain_nets)
        if rec.is_input:
            # Core input net -> controllable pseudo-PI. Removing the wrapper cell
            # leaves core_net driver-less; the new input port drives it.
            port = f"{WBI_PREFIX}{rec.instance}"
            cells.pop(rec.instance, None)
            _add_port(module, port, "input", rec.core_net)
            sys_nets_to_drop.add(rec.sys_net)
            wbr_port_map[rec.instance] = {
                "port": port,
                "role": "ppi",
                "core_net": rec.core_net,
                "sys_net": rec.sys_net,
            }
        else:
            # Core output net -> observed pseudo-PO via an observe buffer. The
            # core logic keeps driving core_net; the buffer taps it to the port.
            obs_bit = next_id
            next_id += 1
            port = f"{WBO_PREFIX}{rec.instance}"
            _add_internal_buf_cell(
                cells,
                f"$wbobserve_{rec.instance}",
                OBSERVE_BUF_CELL,
                rec.core_net,
                obs_bit,
            )
            cells.pop(rec.instance, None)
            _add_port(module, port, "output", obs_bit)
            sys_nets_to_drop.add(rec.sys_net)
            wbr_port_map[rec.instance] = {
                "port": port,
                "role": "ppo",
                "core_net": rec.core_net,
                "sys_net": rec.sys_net,
            }

    # Second pass: remove constant-FROM_CORE WBR out cells that extract_wbr_cells
    # skipped (they can't be INTEST observation sites, but they must be stripped so
    # the fused view is purely combinational for the C++ SAT engine).
    for instance in list(cells):
        cell = cells.get(instance)
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("type")
        if ctype not in (_WBR_OUT_TYPES | _WBR_SCAN_OUT_TYPES):
            continue
        if not _is_constant_bit(cell, _WBR_OUT_CORE_PIN):
            continue
        # Skipped by extract_wbr_cells: drop it and decouple sys/chain ports.
        conns = cell.get("connections", {})
        if isinstance(conns, dict):
            sys_nets_to_drop.update(_all_int_bits(conns.get(_WBR_OUT_SYS_PIN)))
            for pin in _WBR_SCAN_CHAIN_PINS:
                sys_nets_to_drop.update(_all_int_bits(conns.get(pin)))
        cells.pop(instance, None)

    _drop_top_ports_by_bit(module, sys_nets_to_drop)

    attrs = module.setdefault("attributes", {})
    if isinstance(attrs, dict):
        attrs["faultflow_wbr_scan_view_schema_ver"] = WBR_SCAN_VIEW_SCHEMA_VER

    return view, wbr_port_map


def _fuse_wbr_extest(
    view: dict[str, Any], top: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """EXTEST fusion: test the interconnect; hold the core dead/safe.

    Symmetric with the INTEST branch but operating on the *system* side of each
    boundary cell. See `fuse_wbr_into_view` for the full role table.
    """
    _, module = _top_module(view, top)
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")

    records = extract_wbr_cells(module)
    next_id = _next_net_id(module)
    wbr_port_map: dict[str, dict[str, Any]] = {}
    chain_nets_to_drop: set[int] = set()

    for rec in records:
        # Scan-model cells leave their chain ports dangling once removed.
        chain_nets_to_drop.update(rec.chain_nets)
        if rec.is_input:
            # OBSERVE the system side: tap sys_net (FROM_SYS) to a new output.
            # SAFE the core side: drop the wrapper and tie core_net (TO_CORE) to 0.
            obs_bit = next_id
            next_id += 1
            port = f"{WBI_OBS_PREFIX}{rec.instance}"
            _add_internal_buf_cell(
                cells,
                f"$wbiobserve_{rec.instance}",
                OBSERVE_BUF_CELL,
                rec.sys_net,
                obs_bit,
            )
            cells.pop(rec.instance, None)
            _add_const0_driver_cell(cells, f"$wbsafe0_{rec.instance}", rec.core_net)
            _add_port(module, port, "output", obs_bit)
            wbr_port_map[rec.instance] = {
                "port": port,
                "role": "ppo",
                "core_net": rec.core_net,
                "sys_net": rec.sys_net,
            }
        else:
            # CONTROL the system side: drop the wrapper and expose sys_net
            # (TO_SYS) as a new input. The core net (FROM_CORE) is left dangling.
            port = f"{WBO_CTL_PREFIX}{rec.instance}"
            cells.pop(rec.instance, None)
            _add_port(module, port, "input", rec.sys_net)
            wbr_port_map[rec.instance] = {
                "port": port,
                "role": "ppi",
                "core_net": rec.core_net,
                "sys_net": rec.sys_net,
            }

    # The core is dead in EXTEST: drop scan observe points (__ppo_*) entirely.
    # __ppi_* inputs feed only dead, unobserved core logic and are left as-is.
    # Top-level PIs/POs are KEPT (EXTEST stimulus / observables).
    _drop_ppo_ports_and_observers(module)
    _drop_top_ports_by_bit(module, chain_nets_to_drop)

    attrs = module.setdefault("attributes", {})
    if isinstance(attrs, dict):
        attrs["faultflow_wbr_scan_view_schema_ver"] = WBR_SCAN_EXTEST_VIEW_SCHEMA_VER

    return view, wbr_port_map
