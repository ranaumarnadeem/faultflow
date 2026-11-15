"""A design with a latch through the scan flows on the X engine, end to end: scan
insertion, scan-check (its normal-mode comparison on the X engine), stuck-at and
transition campaigns (launch on capture and on shift), plain and as the IEEE 1500
wrapper's INTEST. Every credit is replayed on the scalar X scan protocol oracle
from the exported patterns (tests/python/x_scan_replay.py).

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
opens, so its value at the capture is unknown -- a possible detection. The
protocol oracle used to load fault-free and did not show it; it now runs a
stuck-at off the scan path in the load, as the netlist with the fault tied in
does."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from x_scan_replay import (
    Design,
    campaign,
    config,
    faults,
    best_with_fault_throughout,
    not_reproduced,
    redundant_detected,
)

SL = Design(
    "sl",
    """
module sl(input clk, input d, input g, input a, input b, output y, output z);
  reg q1, q2, l;
  always @(posedge clk) q1 <= a ^ b;
  always @* if (g) l = d & q1;
  always @(posedge clk) q2 <= l | a;
  assign y = q2 ^ b;
  assign z = l & b;
endmodule
""",
    ("a", "b", "d", "g"),
)


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


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
        cfg = config(work, SL, engine)
        Runner(cfg).init()
        Runner(cfg).scan()
        if engine == "x":
            Runner(cfg).scan_check()
            manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
            assert manifest["latest_check"]["status"] == "PASS"
            netlist = json.loads(Path(str(manifest["generic_json"])).read_text())
            types = {c["type"] for c in netlist["modules"][SL.top]["cells"].values()}
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
    monkeypatch.chdir(tmp_path)
    cfg, patterns = campaign(tmp_path, SL, model, launch)
    assert faults(cfg, ("detected",))
    assert not_reproduced(cfg, SL.top, patterns, model, launch, intest=False) == []
    assert redundant_detected(cfg, SL, model, launch) == []


@pytest.mark.integration
@pytest.mark.parametrize("launch", ["loc", "los"])
def test_intest_of_a_latch_design_credits_what_the_protocol_shows(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch, launch: str
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, patterns = campaign(tmp_path, SL, "transition", launch, intest=True)
    assert faults(cfg, ("detected",))
    missing = not_reproduced(cfg, SL.top, patterns, "transition", launch, True)
    assert missing == []


@pytest.mark.integration
def test_intest_stuck_at_on_a_latch_design(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every credit reproduced by the protocol, each possible one -- the latch's
    enable stuck closed, so it never opens -- too, and possible on the netlist
    with the fault tied in (acting in the load, as on the chip); none left
    undetected detected definitely there."""
    monkeypatch.chdir(tmp_path)
    cfg, patterns = campaign(tmp_path, SL, "stuck_at", "loc", intest=True)
    assert not_reproduced(cfg, SL.top, patterns, "stuck_at", "loc", intest=True) == []
    assert faults(cfg, ("possible",))
    for key, kind, status in faults(cfg, ("possible", "undetected")):
        best = best_with_fault_throughout(cfg, SL.top, patterns, key, kind, intest=True)
        if status == "possible":
            assert best == "possible", (key, kind)
        else:
            assert best != "definite", (key, kind)
