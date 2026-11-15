"""A design with a latch through the scan flows on the X engine, end to end: scan
insertion, scan-check (its normal-mode comparison on the X engine), stuck-at and
transition campaigns (launch on capture and on shift), plain and as the IEEE 1500
wrapper's INTEST. Every credit is replayed on the scalar X scan protocol oracle
from the exported patterns.

sl: q1 <= a ^ b; latch l open while g is 1, l = d & q1; q2 <= l | a;
y = q2 ^ b; z = l & b. In INTEST, g comes from its input cell, which changes as
the wrapper chain shifts: the latch is opened and closed by the load, so at the
capture it holds a value the load left. Two things follow, both tested:

- a transition pattern's expected unload is the capture frame of the launch and
  capture pair (the latch keeps its launch value), not the capture frame alone,
  which would leave the latch unknown and its unload bits uncompared;
- a transition fault the view only possibly detects is decided by the protocol:
  the view powers the latch up unknown at the launch, the chip has the load's
  value there, the same in the good and faulty machine (a delay fault is
  inactive in the slow shift).

A stuck-at acts in the load too: with the latch's enable stuck closed it never
opens, so its value at the capture is unknown -- a possible detection the
protocol oracle (whose load is fault-free) does not show and a netlist with the
fault tied in does."""

from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
CELL_LIB = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
TOP = "sl"
RTL = """
module sl(input clk, input d, input g, input a, input b, output y, output z);
  reg q1, q2, l;
  always @(posedge clk) q1 <= a ^ b;
  always @* if (g) l = d & q1;
  always @(posedge clk) q2 <= l | a;
  assign y = q2 ^ b;
  assign z = l & b;
endmodule
"""
RANK = {"none": 0, "possible": 1, "definite": 2}


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


def _config(
    work: Path,
    engine: str,
    model: str = "stuck_at",
    launch: str = "loc",
    wrap: bool = False,
) -> Any:
    from faultflow.config import load_config

    src = work / "sl.v"
    src.write_text(RTL, encoding="utf-8")
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
    return load_config(path, TOP)


def _campaign(
    work: Path, model: str, launch: str, intest: bool = False
) -> tuple[Any, Path]:
    from faultflow.runner import Runner

    cfg = _config(work, "x", model, launch, wrap=intest)
    Runner(cfg).init()
    Runner(cfg).scan()
    Runner(cfg).scan_check()
    patterns = work / "patterns.json"
    mode_cfg = dataclasses.replace(cfg, test_mode="intest") if intest else cfg
    Runner(mode_cfg).sim(scan=True, clean=True, export_patterns=patterns)
    return cfg, patterns


@dataclasses.dataclass
class _Protocol:
    """The X scan protocol over the netlist the campaign grades: the scanned
    design, or INTEST's harness with whatever drives the block unknown."""

    core: Any
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


def _protocol(cfg: Any, intest: bool) -> _Protocol:
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.scan.x_mask import instance_output_nets
    from faultflow.wrap.environment import with_environment

    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    view = json.loads((cfg.intermediate_dir / "scan_atpg_view.json").read_text())
    outputs = [
        name
        for name, port in view["modules"][TOP]["ports"].items()
        if port["direction"] == "output" and not name.startswith("__")
    ]
    if intest:
        harness = cfg.intermediate_dir / f"{TOP}_intest.json"
        icfg = with_environment(dataclasses.replace(cfg, test_mode="intest"))
        module = json.loads(harness.read_text())["modules"][TOP]
        unknown = list(instance_output_nets(module, icfg.blackbox_x_instances))
        netlist, blackboxes = harness, list(icfg.blackbox_instances)
    else:
        netlist, blackboxes, unknown = Path(str(manifest["generic_json"])), [], []
    return _Protocol(
        _load_core(),
        netlist,
        str(resolve_scan_cell_map(cfg)),
        manifest,
        outputs,
        blackboxes,
        unknown,
        cfg.simulation.graph_policy,
    )


def _faults(cfg: Any, statuses: tuple[str, ...]) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(cfg.db_path)
    try:
        (campaign,) = conn.execute(
            "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
        ).fetchone()
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(
            "SELECT fault_site_key, fault_type, status FROM faults WHERE "
            f"campaign_id = ? AND status IN ({marks}) AND collapsed_into IS NULL",
            (campaign, *statuses),
        ).fetchall()
    finally:
        conn.close()
    return [(str(k), str(t), str(s)) for k, t, s in rows]


def _best_outcomes(
    protocol: _Protocol,
    faults: list[tuple[str, str, str]],
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
        protocol.core,
        protocol.netlist,
        protocol.cell_map,
        protocol.policy,
        protocol.blackboxes,
    )
    specs = [(index[key], fault_type_to_sa_code(kind)) for key, kind, _ in faults]
    best = ["none"] * len(faults)
    for pattern in patterns:
        result = protocol.core.simulate_scan_protocol_faults_x(
            str(protocol.netlist),
            protocol.cell_map,
            faults=specs,
            unload_mask=pattern.unload_mask or {},
            reference=True,
            **protocol.kwargs(pattern, model, launch),
        )
        for i, lane in enumerate(result["lanes"]):
            if RANK[str(lane["outcome"])] > RANK[best[i]]:
                best[i] = str(lane["outcome"])
    return best


def _exported(patterns_path: Path) -> list[Any]:
    from faultflow.scan.pattern_export import scan_pattern_from_dict

    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    assert raw, "no patterns exported"
    return [scan_pattern_from_dict(item) for item in raw]


def _not_reproduced(
    cfg: Any, patterns_path: Path, model: str, launch: str, intest: bool
) -> list[tuple[str, str, str, str]]:
    """Each credit no exported pattern shows: 'detected' not definite in any,
    'possible' not even possible."""
    credited = _faults(cfg, ("detected", "possible"))
    assert credited
    best = _best_outcomes(
        _protocol(cfg, intest), credited, _exported(patterns_path), model, launch
    )
    return [
        (key, kind, status, best[i])
        for i, (key, kind, status) in enumerate(credited)
        if (status == "detected" and best[i] != "definite")
        or (status == "possible" and best[i] == "none")
    ]


def _shown_with_fault_throughout(
    cfg: Any, patterns_path: Path, site_key: str, fault_type: str
) -> bool:
    """A stuck-at on a branch `net:<n>:branch:<cell>:<pin>`, tied into INTEST's
    harness so it acts in every cycle, the load included: does some exported
    pattern show a compared bit known in the good machine and unknown or
    different in the faulty one?"""
    _, _, kind, cell, pin = site_key.split(":")
    assert kind == "branch", site_key
    protocol = _protocol(cfg, intest=True)
    netlist = json.loads(protocol.netlist.read_text(encoding="utf-8"))
    netlist["modules"][TOP]["cells"][cell]["connections"][pin] = [
        "1" if fault_type == "sa1" else "0"
    ]
    faulty = protocol.netlist.with_name("faulty_harness.json")
    faulty.write_text(json.dumps(netlist), encoding="utf-8")
    for pattern in _exported(patterns_path):
        kwargs = protocol.kwargs(pattern, "stuck_at", "loc")
        good = protocol.core.simulate_scan_pattern_x(
            str(protocol.netlist), protocol.cell_map, **kwargs
        )
        bad = protocol.core.simulate_scan_pattern_x(
            str(faulty), protocol.cell_map, **kwargs
        )
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


@pytest.mark.integration
def test_scan_check_compares_a_latch_design_on_the_x_engine(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.runner import Runner
    from faultflow.runner.runner import RunnerError

    for engine in ("x", "two_valued"):
        work = tmp_path / engine
        work.mkdir()
        monkeypatch.chdir(work)
        cfg = _config(work, engine)
        Runner(cfg).init()
        Runner(cfg).scan()
        if engine == "x":
            Runner(cfg).scan_check()
            manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
            assert manifest["latest_check"]["status"] == "PASS"
            netlist = json.loads(Path(str(manifest["generic_json"])).read_text())
            types = {c["type"] for c in netlist["modules"][TOP]["cells"].values()}
            assert "sky130_fd_sc_hd__dlxtp_1" in types
        else:
            # The two-valued engine models no latch, and says which engine does.
            with pytest.raises(RunnerError, match=r"(?i)a latch.*engine = x"):
                Runner(cfg).scan_check()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("model", "launch"),
    [("stuck_at", "loc"), ("transition", "loc"), ("transition", "los")],
)
def test_a_latch_designs_scan_credit_is_the_protocols(
    tmp_path: Path,
    flow_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    launch: str,
) -> None:
    """Every credit reproduced; a fault left redundant is detected by no load
    and capture at all (the design is small enough to try every one)."""
    from faultflow.scan.protocol import ScanPattern
    from faultflow.scan.protocol_constants import load_sequence

    monkeypatch.chdir(tmp_path)
    cfg, patterns = _campaign(tmp_path, model, launch)
    assert _faults(cfg, ("detected",))
    assert _not_reproduced(cfg, patterns, model, launch, intest=False) == []
    redundant = _faults(cfg, ("redundant",))
    if not redundant:
        return
    protocol = _protocol(cfg, intest=False)
    chains = range(len(protocol.manifest["scan_inputs"]))
    length = int(protocol.manifest["max_chain_length"])
    inputs = ["a", "b", "d", "g"]
    every = []
    for load in itertools.product([False, True], repeat=length * len(chains)):
        for values in itertools.product([False, True], repeat=len(inputs)):
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
                        capture_pi_values=dict(zip(inputs, values)),
                        expected_unload={},
                        launch_scan_in={c: head for c in chains},
                    )
                )
    best = _best_outcomes(protocol, redundant, every, model, launch)
    assert best == ["none"] * len(redundant)


@pytest.mark.integration
@pytest.mark.parametrize("launch", ["loc", "los"])
def test_intest_of_a_latch_design_credits_what_the_protocol_shows(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch, launch: str
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, patterns = _campaign(tmp_path, "transition", launch, intest=True)
    assert _faults(cfg, ("detected",))
    assert _not_reproduced(cfg, patterns, "transition", launch, intest=True) == []


@pytest.mark.integration
def test_intest_stuck_at_on_a_latch_design(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every definite credit reproduced by the protocol; each possible one --
    the latch's enable stuck closed, so it never opens -- shown by the netlist
    with the fault tied in."""
    monkeypatch.chdir(tmp_path)
    cfg, patterns = _campaign(tmp_path, "stuck_at", "loc", intest=True)
    missing = _not_reproduced(cfg, patterns, "stuck_at", "loc", intest=True)
    assert [row for row in missing if row[2] == "detected"] == []
    for key, kind, _, _ in missing:
        assert _shown_with_fault_throughout(cfg, patterns, key, kind), (key, kind)
