"""A small design through the X engine's scan flows, its credit replayed on the
scalar X scan protocol oracle from the exported patterns
(simulate_scan_protocol_faults_x, reference=True).

A design is RTL with one clock, ``clk``, synthesized by the campaign itself;
``inputs`` are its other inputs (an exhaustive search tries every value of them
with every load). Used by the latch, loop and bus scan tests."""

from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
RANK = {"none": 0, "possible": 1, "definite": 2}


@dataclasses.dataclass(frozen=True)
class Design:
    top: str
    rtl: str
    inputs: tuple[str, ...]


def config(
    work: Path,
    design: Design,
    engine: str,
    model: str = "stuck_at",
    launch: str = "loc",
    wrap: bool = False,
) -> Any:
    from faultflow.config import load_config

    src = work / f"{design.top}.v"
    src.write_text(design.rtl, encoding="utf-8")
    (work / "schemas").mkdir(exist_ok=True)
    shutil.copy(ROOT / "schemas/coverage.schema.json", work / "schemas")
    path = work / "config.ofs"
    path.write_text(
        f"[design]\nnetlist = {src}\ncell_lib = {CELL_LIB}\nliberty = {LIBERTY}\n"
        f"[fault_model]\ncollapsing = false\nmodel = {model}\nlaunch = {launch}\n"
        f"[simulation]\nunsupported_cells = fail\nengine = {engine}\n"
        "[atpg]\nrandom_vectors = 32\nmax_rounds = 10\n[scan]\nchains = 1\n"
        + ("[wrap]\nenabled = true\n" if wrap else ""),
        encoding="utf-8",
    )
    return load_config(path, design.top)


def campaign(
    work: Path, design: Design, model: str, launch: str, intest: bool = False
) -> tuple[Any, Path]:
    """Scan insertion, scan-check and a scan campaign on the X engine (INTEST's
    with `intest`), its patterns exported."""
    from faultflow.runner import Runner

    cfg = config(work, design, "x", model, launch, wrap=intest)
    Runner(cfg).init()
    Runner(cfg).scan()
    Runner(cfg).scan_check()
    patterns = work / "patterns.json"
    mode_cfg = dataclasses.replace(cfg, test_mode="intest") if intest else cfg
    Runner(mode_cfg).sim(scan=True, clean=True, export_patterns=patterns)
    return cfg, patterns


@dataclasses.dataclass
class Protocol:
    """The X scan protocol over the netlist the campaign grades: the scanned
    design, or INTEST's harness with whatever drives the block unknown."""

    core: Any
    top: str
    netlist: Path
    cell_map: str
    manifest: dict[str, Any]
    outputs: list[str]
    blackboxes: list[str]
    unknown_nets: list[int]
    policy: str

    def kwargs(self, pattern: Any, model: str, launch: str) -> dict[str, Any]:
        from faultflow.scan.manifest import manifest_clock_ports
        from faultflow.scan.setup import setup_request

        transition = model == "transition"
        return dict(
            clock_ports=manifest_clock_ports(self.manifest, self.netlist),
            scan_enable_port=str(self.manifest["scan_enable"]),
            scan_input_ports=[str(p) for p in self.manifest["scan_inputs"]],
            scan_output_ports=[str(p) for p in self.manifest["scan_outputs"]],
            functional_output_ports=self.outputs,
            max_chain_length=int(self.manifest["max_chain_length"]),
            load_seqs=pattern.load_seqs,
            capture_pi_values=pattern.capture_pi_values,
            unsupported_policy=self.policy,
            blackbox_instances=self.blackboxes,
            preamble_cycles=pattern.preamble_cycles,
            shift_pi_values=dict(pattern.shift_pi_values),
            setup_cycles=setup_request(pattern.setup),
            unknown_nets=self.unknown_nets,
            loc_two_capture=transition and launch == "loc",
            los_two_capture=transition and launch == "los",
            los_launch_scan_in={
                int(k): bool(v) for k, v in pattern.launch_scan_in.items()
            },
        )


def protocol(cfg: Any, top: str, intest: bool) -> Protocol:
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.scan.x_mask import instance_output_nets
    from faultflow.wrap.environment import with_environment

    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    view = json.loads((cfg.intermediate_dir / "scan_atpg_view.json").read_text())
    outputs = [
        name
        for name, port in view["modules"][top]["ports"].items()
        if port["direction"] == "output" and not name.startswith("__")
    ]
    if intest:
        harness = cfg.intermediate_dir / f"{top}_intest.json"
        icfg = with_environment(dataclasses.replace(cfg, test_mode="intest"))
        module = json.loads(harness.read_text())["modules"][top]
        unknown = list(instance_output_nets(module, icfg.blackbox_x_instances))
        netlist, blackboxes = harness, list(icfg.blackbox_instances)
    else:
        netlist, blackboxes, unknown = Path(str(manifest["generic_json"])), [], []
    return Protocol(
        _load_core(),
        top,
        netlist,
        str(resolve_scan_cell_map(cfg)),
        manifest,
        outputs,
        blackboxes,
        unknown,
        cfg.simulation.graph_policy,
    )


def faults(cfg: Any, statuses: tuple[str, ...]) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(cfg.db_path)
    try:
        (campaign_id,) = conn.execute(
            "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
        ).fetchone()
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(
            "SELECT fault_site_key, fault_type, status FROM faults WHERE "
            f"campaign_id = ? AND status IN ({marks}) AND collapsed_into IS NULL",
            (campaign_id, *statuses),
        ).fetchall()
    finally:
        conn.close()
    return [(str(k), str(t), str(s)) for k, t, s in rows]


def best_outcomes(
    proto: Protocol,
    fault_rows: list[tuple[str, str, str]],
    patterns: list[Any],
    model: str,
    launch: str,
) -> list[str]:
    """Per fault, its best X protocol outcome over `patterns` (the scalar
    reference): definite, possible or none."""
    from faultflow.scan.site_resolution import (
        build_site_key_index,
        fault_type_to_sa_code,
    )

    index = build_site_key_index(
        proto.core, proto.netlist, proto.cell_map, proto.policy, proto.blackboxes
    )
    specs = [(index[key], fault_type_to_sa_code(kind)) for key, kind, _ in fault_rows]
    best = ["none"] * len(fault_rows)
    for pattern in patterns:
        result = proto.core.simulate_scan_protocol_faults_x(
            str(proto.netlist),
            proto.cell_map,
            faults=specs,
            unload_mask=pattern.unload_mask or {},
            reference=True,
            **proto.kwargs(pattern, model, launch),
        )
        for i, lane in enumerate(result["lanes"]):
            if RANK[str(lane["outcome"])] > RANK[best[i]]:
                best[i] = str(lane["outcome"])
    return best


def exported(patterns_path: Path) -> list[Any]:
    from faultflow.scan.pattern_export import scan_pattern_from_dict

    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    assert raw, "no patterns exported"
    return [scan_pattern_from_dict(item) for item in raw]


def not_reproduced(
    cfg: Any, top: str, patterns_path: Path, model: str, launch: str, intest: bool
) -> list[tuple[str, str, str, str]]:
    """Each credit no exported pattern shows: 'detected' not definite in any,
    'possible' not even possible."""
    credited = faults(cfg, ("detected", "possible"))
    assert credited
    best = best_outcomes(
        protocol(cfg, top, intest), credited, exported(patterns_path), model, launch
    )
    return [
        (key, kind, status, best[i])
        for i, (key, kind, status) in enumerate(credited)
        if (status == "detected" and best[i] != "definite")
        or (status == "possible" and best[i] == "none")
    ]


def redundant_detected(
    cfg: Any, design: Design, model: str, launch: str
) -> list[tuple[str, str, str]]:
    """Each fault left redundant that some load and capture detects, over every
    load and every value of the design's inputs (and, launching on shift, every
    launch scan-in bit). Plain scan only."""
    from faultflow.scan.protocol import ScanPattern
    from faultflow.scan.protocol_constants import load_sequence

    redundant = faults(cfg, ("redundant",))
    if not redundant:
        return []
    proto = protocol(cfg, design.top, intest=False)
    chains = range(len(proto.manifest["scan_inputs"]))
    length = int(proto.manifest["max_chain_length"])
    every = []
    for load in itertools.product([False, True], repeat=length * len(chains)):
        for values in itertools.product([False, True], repeat=len(design.inputs)):
            for head in [False, True] if launch == "los" else [False]:
                every.append(
                    ScanPattern(
                        load_seqs={
                            c: load_sequence(
                                dict(enumerate(load[c * length : (c + 1) * length])),
                                length,
                            )
                            for c in chains
                        },
                        capture_pi_values=dict(zip(design.inputs, values)),
                        expected_unload={},
                        launch_scan_in={c: head for c in chains},
                    )
                )
    best = best_outcomes(proto, redundant, every, model, launch)
    return [fault for fault, outcome in zip(redundant, best) if outcome != "none"]


def _tie(module: dict[str, Any], cell_map: Any, site_key: str, value: str) -> None:
    """Ties a stuck-at's site into `module`: a branch's reader pin, or every
    reader of a stem (`net:<n>:stem`, `net:<n>:branch:<cell>:<pin>`)."""
    from faultflow.control_trace import Netlist

    head, _, rest = site_key.partition(":")
    net_text, _, rest = rest.partition(":")
    assert head == "net", site_key
    net = int(net_text)
    netlist = Netlist(module, cell_map)
    if rest == "stem":
        assert not any(
            net in port["bits"]
            for port in module["ports"].values()
            if port["direction"] != "input"
        ), f"{site_key}: an output port's net"
        readers = [
            (instance, pin)
            for instance, cell in netlist.cells.items()
            for pin, bits in cell.get("connections", {}).items()
            if net in bits
            and netlist.direction(instance, pin, netlist.entry(instance)) == "input"
        ]
    else:
        kind, _, rest = rest.partition(":")
        cell, _, pin = rest.rpartition(":")
        assert kind == "branch" and cell in netlist.cells, site_key
        readers = [(cell, pin)]
    assert readers, site_key
    for instance, pin in readers:
        bits = module["cells"][instance]["connections"][pin]
        module["cells"][instance]["connections"][pin] = [
            value if bit == net else bit for bit in bits
        ]


def shown_with_fault_throughout(
    cfg: Any,
    top: str,
    patterns_path: Path,
    site_key: str,
    fault_type: str,
    intest: bool,
) -> bool:
    """A stuck-at tied into the netlist the campaign grades, so it acts in every
    cycle, the load included (the protocol oracle's load is fault-free): does
    some exported pattern show a compared bit known in the good machine and
    unknown or different in the faulty one?"""
    proto = protocol(cfg, top, intest)
    netlist = json.loads(proto.netlist.read_text(encoding="utf-8"))
    cell_map = json.loads(Path(proto.cell_map).read_text(encoding="utf-8"))
    _tie(
        netlist["modules"][top], cell_map, site_key, "1" if fault_type == "sa1" else "0"
    )
    faulty = proto.netlist.with_name("faulty_tied.json")
    faulty.write_text(json.dumps(netlist), encoding="utf-8")
    for pattern in exported(patterns_path):
        kwargs = proto.kwargs(pattern, "stuck_at", "loc")
        good = proto.core.simulate_scan_pattern_x(
            str(proto.netlist), proto.cell_map, **kwargs
        )
        bad = proto.core.simulate_scan_pattern_x(str(faulty), proto.cell_map, **kwargs)
        for port, value in dict(good["real_po_values"]).items():
            if value is not None and bad["real_po_values"][port] != value:
                return True
        mask = pattern.unload_mask or {}
        for chain, bits in dict(good["unload_seqs"]).items():
            for t, value in enumerate(bits):
                compared = chain not in mask or t >= len(mask[chain]) or mask[chain][t]
                if (
                    compared
                    and value is not None
                    and bad["unload_seqs"][chain][t] != value
                ):
                    return True
    return False
