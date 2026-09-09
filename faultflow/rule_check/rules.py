"""DFT rule set. Each rule reads the structural NetlistFacts (and, when present,
the scan manifest) and returns Violations. New structural rules live here;
scan-chain rules reuse the existing `check_scan_structure`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from faultflow.rule_check.facts import CellFact, NetlistFacts, build_netlist_facts
from faultflow.rule_check.model import RuleCheckReport, Severity, Violation


def _cell_index(facts: NetlistFacts) -> dict[str, CellFact]:
    return {c.instance: c for c in facts.cells}


def _net(facts: NetlistFacts, net: int) -> str:
    return f"{facts.net_name(net)} (net {net})"


# --- CLK003: informational — multi-clock is fully supported ---
def rule_multiple_clock_domains(facts: NetlistFacts) -> list[Violation]:
    if len(facts.clock_nets) <= 1:
        return []
    ff_per_domain: dict[int, int] = {n: 0 for n in facts.clock_nets}
    for cell in facts.cells:
        if cell.is_ff and cell.clock_net in ff_per_domain:
            ff_per_domain[cell.clock_net] += 1

    def _dom(n: int) -> str:
        count = ff_per_domain[n]
        label = "FF" if count == 1 else "FFs"
        return f"{facts.net_name(n)} ({count} {label})"

    domain_strs = ", ".join(_dom(n) for n in sorted(facts.clock_nets))
    return [
        Violation(
            "CLK003",
            Severity.INFO,
            "multiple clock domains",
            f"design has {len(facts.clock_nets)} clock domains: {domain_strs}; "
            "stuck-at, scan ATPG, and per-domain at-speed transition are supported",
        )
    ]


# --- CLK004: informational — FF->FF cross-domain data paths ---
def rule_cross_domain_data_paths(facts: NetlistFacts) -> list[Violation]:
    """Cross-domain FF->FF paths are masked during per-domain at-speed transition."""
    if len(facts.clock_nets) <= 1:
        return []
    cell_by_inst: dict[str, CellFact] = {c.instance: c for c in facts.cells}

    def _ff_d_reachable(start_net: int) -> set[tuple[str, str]]:
        visited: set[int] = set()
        worklist = [start_net]
        reached: set[tuple[str, str]] = set()
        while worklist:
            net = worklist.pop()
            if net in visited:
                continue
            visited.add(net)
            for sink_inst, sink_pin in facts.sinks_of.get(net, []):
                sink = cell_by_inst.get(sink_inst)
                if sink is None:
                    continue
                if sink.is_ff:
                    if sink_pin != sink.clock_pin:
                        reached.add((sink_inst, sink_pin))
                else:
                    worklist.extend(sink.outputs.values())
        return reached

    seen: set[tuple[str, str]] = set()
    cross_pairs: list[tuple[str, str]] = []
    for cell in facts.cells:
        if not cell.is_ff:
            continue
        for q_net in cell.outputs.values():
            for dst_inst, _pin in _ff_d_reachable(q_net):
                dst = cell_by_inst.get(dst_inst)
                if dst is None or not dst.is_ff:
                    continue
                if dst.clock_net != cell.clock_net:
                    pair = (cell.instance, dst_inst)
                    if pair not in seen:
                        seen.add(pair)
                        cross_pairs.append(pair)

    if not cross_pairs:
        return []

    def _pair_str(src: str, dst: str) -> str:
        return f"{src}->{dst}"

    pair_strs = ", ".join(_pair_str(s, d) for s, d in sorted(cross_pairs))
    return [
        Violation(
            "CLK004",
            Severity.INFO,
            "cross-domain data paths",
            f"{len(cross_pairs)} cross-domain FF->FF data path(s): {pair_strs}; "
            "masked during per-domain at-speed transition ATPG",
        )
    ]


# --- CLK001: every FF clock must be controllable from a primary input ---
def _clock_controllable(
    facts: NetlistFacts, cells: dict[str, CellFact], clock_net: int
) -> bool:
    cur = clock_net
    seen: set[int] = set()
    while True:
        if cur in facts.pis:
            return True
        if cur in seen:
            return False
        seen.add(cur)
        drv = facts.driver_of.get(cur)
        if drv is None:
            return False
        cell = cells.get(drv[0])
        # Transparent only through a pure single-input buffer/inverter clock tree.
        if cell is not None and cell.is_buffer and len(cell.inputs) == 1:
            cur = next(iter(cell.inputs.values()))
            continue
        return False


def rule_uncontrollable_clock(facts: NetlistFacts) -> list[Violation]:
    cells = _cell_index(facts)
    out: list[Violation] = []
    for clock_net in sorted(facts.clock_nets):
        if _clock_controllable(facts, cells, clock_net):
            continue
        drv = facts.driver_of.get(clock_net)
        src = f"driven by {drv[0]}" if drv else "undriven internal net"
        out.append(
            Violation(
                "CLK001",
                Severity.ERROR,
                "uncontrollable clock",
                f"clock {_net(facts, clock_net)} is not controllable from a PI "
                f"({src}); the scan/capture protocol cannot pulse it",
            )
        )
    return out


# --- CLK002: a clock net must not also be consumed as data/logic ---
def rule_clock_as_data(facts: NetlistFacts) -> list[Violation]:
    cells = _cell_index(facts)
    out: list[Violation] = []
    for clock_net in sorted(facts.clock_nets):
        for inst, pin in facts.sinks_of.get(clock_net, []):
            cell = cells.get(inst)
            if cell is None:
                continue
            if cell.is_ff and pin == cell.clock_pin:
                continue  # legitimate clock use
            if cell.is_buffer:
                continue  # clock distribution buffer
            out.append(
                Violation(
                    "CLK002",
                    Severity.WARNING,
                    "clock-as-data",
                    f"clock {_net(facts, clock_net)} also drives data pin "
                    f"{inst}.{pin} ({cell.cell_type})",
                )
            )
    return out


# --- NET001: every net has exactly one driver (no bus contention) ---
def rule_multi_driver(facts: NetlistFacts) -> list[Violation]:
    out: list[Violation] = []
    for net, drivers in sorted(facts.multi_driven.items()):
        out.append(
            Violation(
                "NET001",
                Severity.ERROR,
                "multi-driver net",
                f"net {_net(facts, net)} is driven by {len(drivers)} cells: "
                + ", ".join(drivers),
            )
        )
    return out


# --- STRUCT001: no combinational feedback loop ---
def _find_comb_cycle(facts: NetlistFacts) -> list[int] | None:
    adj: dict[int, list[int]] = {}
    for cell in facts.cells:
        if cell.is_ff:
            continue  # FF Q is state, not a combinational dependency
        for inet in cell.inputs.values():
            for onet in cell.outputs.values():
                if inet == onet:
                    continue
                adj.setdefault(inet, []).append(onet)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = {}
    for root in list(adj.keys()):
        if color.get(root, WHITE) != WHITE:
            continue
        stack: list[tuple[int, int]] = [(root, 0)]
        path: list[int] = []
        color[root] = GRAY
        path.append(root)
        while stack:
            node, idx = stack[-1]
            succs = adj.get(node, [])
            if idx < len(succs):
                stack[-1] = (node, idx + 1)
                nxt = succs[idx]
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    start = path.index(nxt)
                    return path[start:] + [nxt]
                if c == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()
    return None


def rule_combinational_feedback(facts: NetlistFacts) -> list[Violation]:
    cycle = _find_comb_cycle(facts)
    if cycle is None:
        return []
    chain = " -> ".join(facts.net_name(n) for n in cycle)
    return [
        Violation(
            "STRUCT001",
            Severity.ERROR,
            "combinational feedback loop",
            f"combinational cycle through nets: {chain}",
        )
    ]


# --- Scan-chain rules: reuse the existing structural checks + FF eligibility ---
def rules_scan(manifest: dict[str, Any]) -> list[Violation]:
    from faultflow.scan.checks import check_scan_structure

    out: list[Violation] = []
    try:
        result = check_scan_structure(manifest)
    except Exception as exc:  # defensive: a malformed manifest is itself a finding
        return [
            Violation(
                "SCAN001",
                Severity.ERROR,
                "scan structure",
                f"scan structural check failed: {exc}",
            )
        ]
    for err in result.errors:
        out.append(Violation("SCAN001", Severity.ERROR, "scan structure", err))
    for warn in result.warnings:
        out.append(Violation("SCAN001", Severity.WARNING, "scan structure", warn))

    ineligible = manifest.get("ineligible_ffs", [])
    if isinstance(ineligible, list):
        for ff in ineligible:
            if not isinstance(ff, dict):
                continue
            out.append(
                Violation(
                    "SCAN010",
                    Severity.WARNING,
                    "non-scannable FF",
                    f"FF {ff.get('instance')} ({ff.get('cell_type')}) is not "
                    f"scannable: {ff.get('reason')}",
                )
            )
    return out


# --- COMP001: scan compression phase-shifter structural check ---
def rules_compression(manifest: dict[str, Any]) -> list[Violation]:
    """Re-derives the ring-generator phase-shifter's XOR structure from the
    synthesized netlist and diffs it against the manifest -- see
    faultflow.scan.compression_checks.check_compression_structure for the
    algorithm and its documented scope (phase-shifter only; the ring
    generator's own feedback-tap structure is not yet checked here).
    No-op (returns []) when compression is absent/disabled in the manifest.
    """
    from faultflow.scan.compression_checks import check_compression_structure

    try:
        result = check_compression_structure(manifest)
    except Exception as exc:  # defensive: a malformed manifest is itself a finding
        return [
            Violation(
                "COMP001",
                Severity.ERROR,
                "compression structure",
                f"compression structural check failed: {exc}",
            )
        ]
    out: list[Violation] = []
    for err in result.errors:
        out.append(Violation("COMP001", Severity.ERROR, "compression structure", err))
    for warn in result.warnings:
        out.append(
            Violation("COMP001", Severity.WARNING, "compression structure", warn)
        )
    return out


def run_rule_check(
    netlist_json: Path,
    cell_map_json: Path,
    top: str,
    manifest: dict[str, Any] | None = None,
) -> RuleCheckReport:
    facts = build_netlist_facts(netlist_json, cell_map_json, top)
    report = RuleCheckReport(top=top)
    report.violations.extend(rule_multiple_clock_domains(facts))
    report.violations.extend(rule_cross_domain_data_paths(facts))
    report.violations.extend(rule_uncontrollable_clock(facts))
    report.violations.extend(rule_clock_as_data(facts))
    report.violations.extend(rule_multi_driver(facts))
    report.violations.extend(rule_combinational_feedback(facts))
    if manifest is not None:
        report.violations.extend(rules_scan(manifest))
        report.violations.extend(rules_compression(manifest))
    return report
