"""Aggregate per-scope coverage into one chip number, with ownership guards.

The chip coverage of a hierarchical project is the disjoint union of each block's
INTEST coverage (``core + WBC inward``) and the assembly's interconnect EXTEST
coverage (``WBC outward + interconnect``). For that union to be trustworthy it must
be both **disjoint** (no fault owned by two scopes) AND **complete** (every chip
fault owned by some scope) — disjointness alone is optimistic, silently dropping the
wrapper ring's dead-in-this-mode faults.

OWNING-ROLE RULE (the heart of it). Ownership is by (WBC pin) SIDE, not by which
scope can observe the fault: a WBC's INWARD pins (``TO_CORE``/``FROM_CORE``) are
always block-owned; its OUTWARD pins (``FROM_SYS``/``TO_SYS``) are always
assembly-owned; non-WBC faults are owned by the scope they live in. So when the
assembly blackboxes a core (making the WBC-inward nets observable as TPs), it *can*
detect those faults but does NOT chip-own them — they are ``foreign`` here and owned
once via the block. A block excludes its WBC-outward (``wbr_decoupled``); the
assembly owns it (``handoff``). Each fault is classified into exactly one of:

    owned              — owning role == this scope's role AND in this denominator
    foreign            — in this denominator but the OTHER role owns it
    handoff            — excluded here (wbr_decoupled) but the OTHER role owns it
    excluded_by_design — clock/reset/scan/blackbox + AU/redundant + collapsed

Guards (on the UNCOLLAPSED canonical fault universe, keyed net-id-independently by
``(boundary_block, boundary_wbc, pin, side, fault_type)`` so a boundary fault is the
same identity in the block and assembly netlists): (1) disjoint tops; (2) no
canonical key chip-owned by two scopes; (3) handoff completeness — every handed-off
WBC fault (a block's WBC-outward, the assembly's WBC-inward) is chip-owned by another
scope, OR independently ``accounted`` for there (excluded_by_design under the SAME
canonical key — e.g. Policy 3's clock/reset exclusion, or fault collapsing folding a
fused-view WBC pin's fault into a surviving equivalent). Both are chip-wide,
decomposition-independent outcomes a flat whole-chip run would reach identically, so
they satisfy completeness without a literal ownership entry; only a handoff with
NEITHER an owner NOR an accounting entry anywhere means the chip number is optimistic
by that wrapper population.

The native scan WBC (``$wbc_out_scan_faultflow`` etc., ``[wrap] wbr_model = scan``,
the default) is what gives these guards real teeth: its ``TO_SYS``/``CTO`` output is
functionally decoupled during block INTEST (excluded ``wbr_decoupled``) and only
becomes testable once the assembly drives it during EXTEST. The gap-closing fixture
in ``tests/python/project/test_project_aggregate.py`` proves this directly: a
wrapper-outward fault excluded (undetectable) in a block's own INTEST campaign is
independently confirmed SAT-detected under the same canonical key in the assembly's
EXTEST campaign.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.db import connect, latest_campaign_id, summary
from faultflow.project.orchestrator import ScopeRun

# WBC cell types: the single source of truth lives in the fusion layer; import
# them so a new wrapper-cell variant is recognized as a boundary in both places.
from faultflow.scan.wbr_view import (
    _WBR_IN_TYPES,
    _WBR_OUT_TYPES,
    _WBR_SCAN_IN_TYPES,
    _WBR_SCAN_OUT_TYPES,
)

# Exclusion tags that hand a fault to the OTHER scope (cross-scope ownership).
# `wbr_decoupled` is the only tag `site_resolution.py` actually produces today; it
# covers both wrapper directions (a single generic "this boundary pin isn't driven
# in this test mode" reason), so there is no separate inward/outward tag to track.
_CROSS_SCOPE_EXCLUSIONS = {
    "wbr_decoupled",
}


class AggregateError(RuntimeError):
    """A guard failed — the chip number would be wrong."""


# A WBC pin is "inward" (core-facing → block-owned) or "outward" (system-facing →
# assembly-owned), independent of which scope happens to observe it.
_INWARD_PINS = {"TO_CORE", "FROM_CORE"}
_OUTWARD_PINS = {"FROM_SYS", "TO_SYS"}


@dataclass(frozen=True)
class ScopeCoverage:
    kind: str
    name: str
    top: str
    campaign_id: int
    denominator: int  # the scope's OWN summary denominator (informational)
    detected: int  # the scope's OWN summary detected (informational)
    owned: int  # faults this scope CHIP-owns (by the owning-role rule)
    owned_detected: int  # chip-owned faults that are detected
    foreign: int  # in this scope's denominator but owned by the OTHER role
    handoff: int  # excluded here (wbr_decoupled) but owned by the OTHER role
    excluded_by_design: int
    total_sites: int
    coverage_percent: float | None


@dataclass
class ChipCoverage:
    project: str
    chip_denominator: int = 0
    chip_detected: int = 0
    chip_coverage_percent: float | None = None
    scopes: list[ScopeCoverage] = field(default_factory=list)
    guards: dict[str, Any] = field(default_factory=dict)


def _int_bits(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [b for b in value if isinstance(b, int)]


def _wbc_pin_index(
    netlist: Path, top: str
) -> dict[int, tuple[str | None, str, str, str]]:
    """Map yosys net id -> (boundary_block, boundary_wbc, pin, side) for every WBC pin.

    A boundary fault maps to the same canonical identity across the block netlist
    (where it was INTEST-tested) and the assembly netlist (where its outward side is
    EXTEST-tested). The tie is the *boundary id*:
      * an assembly wrapper carries ``attributes.faultflow_block`` /
        ``faultflow_wbc`` naming the block + block-WBC instance it represents;
      * a block's own wrapper has no such attributes, so its boundary id defaults to
        ``(scope_name, instance)`` (resolved by the caller).
    `boundary_block` is None when untagged (use the scope name). `side` is "in"
    (WBR_IN) or "out" (WBR_OUT).
    """
    data = json.loads(netlist.read_text(encoding="utf-8"))
    modules = data.get("modules", {})
    module = modules.get(top)
    if not isinstance(module, dict):
        return {}
    cells = module.get("cells", {})
    if not isinstance(cells, dict):
        return {}
    index: dict[int, tuple[str | None, str, str, str]] = {}
    for inst, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("type")
        if ctype in _WBR_IN_TYPES or ctype in _WBR_SCAN_IN_TYPES:
            side = "in"
        elif ctype in _WBR_OUT_TYPES or ctype in _WBR_SCAN_OUT_TYPES:
            side = "out"
        else:
            continue
        attrs = cell.get("attributes", {})
        attrs = attrs if isinstance(attrs, dict) else {}
        block = attrs.get("faultflow_block")
        boundary_wbc = str(attrs.get("faultflow_wbc", inst))
        boundary_block = str(block) if isinstance(block, str) and block else None
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for pin, bits in conns.items():
            for bit in _int_bits(bits):
                index[bit] = (boundary_block, boundary_wbc, str(pin), side)
    return index


def _cell_boundary_pin(
    cell: dict[str, Any], inst: str
) -> tuple[str | None, str, str, str] | None:
    """Resolve a single WBC cell's own (boundary_block, boundary_wbc, pin, side)
    from its `type` + `attributes`, independent of any net id.

    Used instead of a net-id-keyed reverse lookup because a system-side net can be
    shared by more than one WBC cell (a genuinely shared control net like `resetn`
    fanning out to several blocks, or nets aliased together by glue-level
    optimization) -- a net-id index can only remember the last cell that claimed
    that id, silently misattributing every earlier one.
    """
    ctype = cell.get("type")
    if ctype in _WBR_IN_TYPES or ctype in _WBR_SCAN_IN_TYPES:
        side, pin = "in", "FROM_SYS"
    elif ctype in _WBR_OUT_TYPES or ctype in _WBR_SCAN_OUT_TYPES:
        side, pin = "out", "TO_SYS"
    else:
        return None
    attrs = cell.get("attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    block = attrs.get("faultflow_block")
    boundary_wbc = str(attrs.get("faultflow_wbc", inst))
    boundary_block = str(block) if isinstance(block, str) and block else None
    return (boundary_block, boundary_wbc, pin, side)


def _wbc_pin_index_extest(
    netlist: Path, top: str
) -> dict[int, tuple[str | None, str, str, str]]:
    """Like `_wbc_pin_index`, but for a scan-model EXTEST scope.

    `_sim_extest` never runs ATPG on the graybox in `netlist` directly -- it runs on
    `fuse_wbr_into_view`'s output, which allocates BRAND NEW net ids for the wrapper
    boundary observe/control ports (`__wbi_obs_*` / `__wbo_ctl_*`), disjoint from the
    graybox's own numbering (confirmed empirically: a composed graybox's
    `FROM_SYS` net 4 became a fresh net 127 after fusion). So `_wbc_pin_index` on the
    graybox can never match a `scan_extest` scope's real fault net ids.

    Fusion is pure and deterministic, so re-run it here and bridge through its
    returned `port_map`, which is keyed by the ORIGINAL graybox cell name -- resolve
    each entry's boundary identity directly from that cell's own attributes
    (`_cell_boundary_pin`) rather than through `sys_net`: `sys_net` is shared
    whenever more than one WBC cell's system-side pin lands on the same graybox net
    (a broadcast control net, or nets merged by glue-level optimization), and a
    net-id-keyed lookup can only resolve to one of them. Only the OUTWARD
    (sys-facing) pins are covered: Guard 3's handoff check only needs those
    (`FROM_SYS`/`TO_SYS` -- what a block excludes as `wbr_decoupled` and the
    assembly must own); the core-facing pins are block-owned regardless and,
    post-fusion, safed/dangling with no live fault site.
    """
    from faultflow.scan.wbr_view import fuse_wbr_into_view

    data = json.loads(netlist.read_text(encoding="utf-8"))
    module = data.get("modules", {}).get(top)
    cells = module.get("cells", {}) if isinstance(module, dict) else {}
    if not isinstance(cells, dict) or not cells:
        return {}
    # Snapshot each WBC cell's own boundary identity BEFORE fusion: fuse_wbr_into_view
    # mutates `data` in place (splicing in the fused pseudo-ports over the originals),
    # so `cells` would no longer hold these entries once it returns.
    cell_pins = {
        cell_name: pin
        for cell_name, cell in cells.items()
        if isinstance(cell, dict)
        and (pin := _cell_boundary_pin(cell, cell_name)) is not None
    }
    fused, port_map = fuse_wbr_into_view(data, top, "extest")
    fused_module = fused.get("modules", {}).get(top, {})
    fused_ports = (
        fused_module.get("ports", {}) if isinstance(fused_module, dict) else {}
    )

    index: dict[int, tuple[str | None, str, str, str]] = {}
    for cell_name, info in port_map.items():
        if not isinstance(info, dict):
            continue
        pin_info = cell_pins.get(cell_name)
        if pin_info is None:
            continue
        port = fused_ports.get(info.get("port"))
        bits = port.get("bits") if isinstance(port, dict) else None
        for bit in _int_bits(bits):
            index[bit] = pin_info
    return index


def _net_id_of(fault_site_key: str, net_id: int) -> int:
    """Prefer the explicit net_id column; fall back to parsing the site key."""
    if net_id >= 0:
        return net_id
    parts = fault_site_key.split(":")
    if len(parts) >= 2 and parts[0] == "net":
        try:
            return int(parts[1])
        except ValueError:
            return -1
    return -1


def _canonical_key(
    scope_name: str,
    fault_site_key: str,
    fault_type: str,
    net_id: int,
    wbc_pins: dict[int, tuple[str | None, str, str, str]],
) -> tuple[Any, ...]:
    """Canonical, net-id-independent fault identity.

    A WBC fault's key is ``(wbc, boundary_block, boundary_wbc, pin, side,
    fault_type)``: it is net-id-independent and shared between the block netlist
    (where the wrapper's inward side is INTEST-owned) and the assembly netlist
    (where the same wrapper's outward side is EXTEST-owned), so a handed-off
    boundary fault is recognised as the same site across scopes. ``boundary_block``
    defaults to the scope name for an untagged (block-local) wrapper. Non-WBC faults
    are scope-local and never collide across scopes.
    """
    nid = _net_id_of(fault_site_key, net_id)
    pin = wbc_pins.get(nid)
    if pin is not None:
        boundary_block = pin[0] or scope_name
        return ("wbc", boundary_block, pin[1], pin[2], pin[3], fault_type)
    return ("local", scope_name, fault_site_key, fault_type)


@dataclass(frozen=True)
class _ScopeKeys:
    owned: set[tuple[Any, ...]]  # keys this scope CHIP-owns (by the owning-role rule)
    handoff: set[tuple[Any, ...]]  # keys owned by the OTHER role (foreign + excluded)
    accounted: set[tuple[Any, ...]]  # WBC keys excluded_by_design here (see below)


def _owning_role(pin: tuple[str | None, str, str, str] | None, scope_role: str) -> str:
    """Which scope role owns a fault: WBC-inward -> block, WBC-outward -> assembly,
    non-WBC -> the scope it lives in. Independent of which scope can observe it."""
    if pin is None:
        return scope_role
    pin_name = pin[2]
    if pin_name in _INWARD_PINS:
        return "block"
    if pin_name in _OUTWARD_PINS:
        return "assembly"
    return scope_role


def _scope_coverage(scope: ScopeRun) -> tuple[ScopeCoverage, _ScopeKeys]:
    """Read one scope's DB + netlist; classify every fault by the owning-role rule.

    A fault is CHIP-owned by this scope iff its owning role == this scope's role AND
    it is in this scope's coverage denominator. The assembly's blackboxed core makes
    the WBC-inward nets observable, so the assembly *can* detect them — but they are
    block-owned (`foreign` here), counted only once via the block. A block's
    WBC-outward is excluded here (`wbr_decoupled`) and owned by the assembly
    (`handoff`).
    """
    if not scope.db_path.exists():
        raise AggregateError(f"scope {scope.name!r}: no database at {scope.db_path}")
    scope_role = "block" if scope.kind == "block" else "assembly"
    # A scan-model EXTEST campaign ran on fuse_wbr_into_view's fused view, not on
    # `scope.netlist` (the pre-fusion graybox) directly -- see
    # _wbc_pin_index_extest's docstring for why the plain (graybox-keyed) index
    # cannot resolve those fault net ids. Combinational EXTEST/INTEST never fuse,
    # so they keep the original, netlist-keyed index unchanged.
    wbc_pins = (
        _wbc_pin_index_extest(scope.netlist, scope.top)
        if scope.campaign_type == "scan_extest"
        else _wbc_pin_index(scope.netlist, scope.top)
    )
    with connect(scope.db_path) as conn:
        conn.row_factory = sqlite3.Row
        campaign_id = latest_campaign_id(conn, scope.campaign_type)
        if campaign_id is None:
            raise AggregateError(
                f"scope {scope.name!r}: no {scope.campaign_type} campaign"
            )
        data = summary(conn, campaign_id=campaign_id)
        rows = conn.execute(
            """
            SELECT fault_site_key, fault_type, net_id, status, exclusion,
                   collapsed_into
            FROM faults WHERE campaign_id = ?
            """,
            (campaign_id,),
        ).fetchall()

    owned = owned_detected = foreign = handoff = excluded_by_design = 0
    owned_keys: set[tuple[Any, ...]] = set()
    handoff_keys: set[tuple[Any, ...]] = set()
    accounted_keys: set[tuple[Any, ...]] = set()
    for row in rows:
        exclusion = str(row["exclusion"])
        status = str(row["status"])
        collapsed = row["collapsed_into"] is not None
        net = int(row["net_id"])
        pin = wbc_pins.get(_net_id_of(str(row["fault_site_key"]), net))
        role = _owning_role(pin, scope_role)
        in_denom = exclusion == "none" and status != "redundant" and not collapsed
        key = _canonical_key(
            scope.name,
            str(row["fault_site_key"]),
            str(row["fault_type"]),
            net,
            wbc_pins,
        )
        if in_denom and role == scope_role:
            owned += 1
            if status == "detected":
                owned_detected += 1
            owned_keys.add(key)
        elif in_denom and role != scope_role:
            # Detectable here, but another role owns it (e.g. assembly's WBC-inward).
            foreign += 1
            handoff_keys.add(key)
        elif exclusion in _CROSS_SCOPE_EXCLUSIONS:
            # Excluded here, owned by the other role (e.g. block's WBC-outward).
            handoff += 1
            if key[0] == "wbc":
                handoff_keys.add(key)
        else:
            # Excluded here for a reason that applies chip-wide regardless of
            # decomposition (clock/reset policy, or collapsed into a surviving
            # equivalent fault). A flat whole-chip run would exclude/collapse the
            # exact same fault the exact same way, so this is not a scope-boundary
            # gap: record it as "accounted for" so Guard 3 doesn't demand a literal
            # owned entry under this identity (see `accounted` below).
            excluded_by_design += 1
            if key[0] == "wbc":
                accounted_keys.add(key)

    total = len(rows)
    if owned + foreign + handoff + excluded_by_design != total:
        raise AggregateError(
            f"scope {scope.name!r}: partition does not cover all {total} fault sites"
        )
    cov = ScopeCoverage(
        kind=scope.kind,
        name=scope.name,
        top=scope.top,
        campaign_id=campaign_id,
        denominator=int(data.get("denominator", 0)),
        detected=int(data.get("detected", 0)),
        owned=owned,
        owned_detected=owned_detected,
        foreign=foreign,
        handoff=handoff,
        excluded_by_design=excluded_by_design,
        total_sites=total,
        coverage_percent=data.get("test_coverage_percent"),
    )
    return cov, _ScopeKeys(
        owned=owned_keys, handoff=handoff_keys, accounted=accounted_keys
    )


def aggregate_project(project_name: str, scopes: list[ScopeRun]) -> ChipCoverage:
    """Disjoint-union the scope coverages into a chip number, enforcing the guards."""
    if not scopes:
        raise AggregateError("project has no scopes to aggregate")

    # Guard 1 — disjoint tops.
    tops = [s.top for s in scopes]
    if len(set(tops)) != len(tops):
        raise AggregateError(f"scope tops are not pairwise distinct: {tops}")

    chip = ChipCoverage(project=project_name)
    all_owned: dict[tuple[Any, ...], str] = {}
    all_handoff: set[tuple[Any, ...]] = set()
    all_accounted: set[tuple[Any, ...]] = set()
    overlaps: list[dict[str, Any]] = []
    for scope in scopes:
        cov, keys = _scope_coverage(scope)
        chip.scopes.append(cov)
        chip.chip_denominator += cov.owned
        chip.chip_detected += cov.owned_detected
        # Guard 2 — no canonical key chip-owned by two scopes (double-count).
        for key in keys.owned:
            prev = all_owned.get(key)
            if prev is not None and prev != scope.name:
                overlaps.append({"key": list(key), "scopes": [prev, scope.name]})
            else:
                all_owned[key] = scope.name
        all_handoff |= keys.handoff
        all_accounted |= keys.accounted

    if overlaps:
        raise AggregateError(f"faults double-counted across scopes: {overlaps}")

    # Guard 3 — chip-wide handoff completeness: every WBC fault a scope hands off
    # (its WBC-outward excluded as wbr_decoupled, or the assembly's WBC-inward that
    # is block-owned) MUST be chip-owned by another scope, UNLESS the same canonical
    # fault is independently `accounted` for there (excluded by clock/reset policy or
    # collapsed into a surviving equivalent — both are chip-wide, decomposition-
    # independent outcomes a flat run would reach identically; see `_scope_coverage`).
    # An unowned, unaccounted handoff means the chip number is OPTIMISTIC by exactly
    # that wrapper population. This is the check that "folds the WBC ring into the
    # assembly fault universe".
    unowned = sorted(
        {
            ":".join(map(str, k[1:]))
            for k in all_handoff
            if k not in all_owned and k not in all_accounted
        }
    )
    if unowned:
        raise AggregateError(
            "wrapper-boundary faults handed off by one scope are owned by no other "
            "scope (the chip number would be optimistic) — the assembly must wrap + "
            f"EXTEST these block boundaries: {unowned}"
        )
    if chip.chip_denominator <= 0:
        raise AggregateError("chip denominator is zero — nothing to cover")

    chip.chip_coverage_percent = 100.0 * chip.chip_detected / chip.chip_denominator
    chip.guards = {
        "tops_disjoint": True,
        "no_double_count": True,
        "partition_total": True,
        "handoff_complete": True,
        "owned_sites": len(all_owned),
        "handoff_sites": len(all_handoff),
        "accounted_sites": len(all_accounted),
    }
    return chip
