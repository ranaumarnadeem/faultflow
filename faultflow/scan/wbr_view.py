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


def _single_bit(cell: dict[str, Any], pin: str, instance: str) -> int:
    conns = cell.get("connections", {})
    bits = _all_int_bits(conns.get(pin)) if isinstance(conns, dict) else []
    if len(bits) != 1:
        raise ScanError(f"wrapper cell {instance}: pin {pin} must be a single net")
    return bits[0]


def extract_wbr_cells(module: dict[str, Any]) -> list[WbrRecord]:
    """All IEEE 1500 boundary cells in `module`, sorted by instance name."""
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        raise ScanError("top module cells must be an object")
    records: list[WbrRecord] = []
    for instance, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("type")
        if ctype in _WBR_IN_TYPES:
            records.append(
                WbrRecord(
                    instance=str(instance),
                    core_net=_single_bit(cell, _WBR_IN_CORE_PIN, str(instance)),
                    sys_net=_single_bit(cell, _WBR_IN_SYS_PIN, str(instance)),
                    is_input=True,
                )
            )
        elif ctype in _WBR_OUT_TYPES:
            records.append(
                WbrRecord(
                    instance=str(instance),
                    core_net=_single_bit(cell, _WBR_OUT_CORE_PIN, str(instance)),
                    sys_net=_single_bit(cell, _WBR_OUT_SYS_PIN, str(instance)),
                    is_input=False,
                )
            )
    return sorted(records, key=lambda r: r.instance)


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

    for rec in records:
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

    attrs = module.setdefault("attributes", {})
    if isinstance(attrs, dict):
        attrs["faultflow_wbr_scan_view_schema_ver"] = WBR_SCAN_EXTEST_VIEW_SCHEMA_VER

    return view, wbr_port_map
