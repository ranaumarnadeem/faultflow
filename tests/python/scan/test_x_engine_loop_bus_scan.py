"""A combinational loop and a tristate bus through the scan flows on the X engine,
end to end: scan insertion, scan-check, stuck-at and transition campaigns (launch
on capture and on shift), plain and as the IEEE 1500 wrapper's INTEST. Every
credit is replayed on the scalar X scan protocol oracle from the exported
patterns (tests/python/x_scan_replay.py).

lp: a set/reset loop between flops (synthesis makes it one a22o reading its own
output): set by q1 & c, reset by q2 & c, holding while c is 0; q3 <= loop ^ a,
z = loop & b. bs: two tristate drivers on one bus, their enables from flops,
so a load can leave the bus driven by one, both or neither.

A transition fault is active from the launch edge: the protocol used to switch
it on at the capture sample, after the launch edge had already let the loop
settle fault-free (a slow reset term reset the loop anyway), and the view's
credit was not reproduced. A stuck-at off the scan path acts in the load too,
as on the chip: c stuck at 0 leaves the loop holding whatever it powered up
with, a possible detection; and a fault the view, which powers the loop up
unknown at the capture, credits only possibly can be a definite detection
through the value the load leaves (hold stuck at 1). The protocol used to load
fault-free and showed neither; the netlist with the fault tied in checks both."""

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

LP = Design(
    "lp",
    """
module lp(input clk, input a, input b, input c, output y, output z);
  reg q1, q2, q3;
  wire s_n, r_n, q, qb;
  always @(posedge clk) begin q1 <= a; q2 <= b; end
  assign s_n = ~(q1 & c);
  assign r_n = ~(q2 & c);
  assign q = ~(s_n & qb);
  assign qb = ~(r_n & q);
  always @(posedge clk) q3 <= q ^ a;
  assign y = q3;
  assign z = q & b;
endmodule
""",
    ("a", "b", "c"),
)
BS = Design(
    "bs",
    """
module bs(input clk, input a, input b, input c, output y, output z);
  reg e1, e2, d1, q;
  wire w;
  always @(posedge clk) begin e1 <= a; e2 <= b; d1 <= a ^ c; end
  assign w = e1 ? d1 : 1'bz;
  assign w = e2 ? c : 1'bz;
  always @(posedge clk) q <= w ^ b;
  assign y = q;
  assign z = w & a;
endmodule
""",
    ("a", "b", "c"),
)
DESIGNS = {"loop": LP, "bus": BS}
FLOWS = [("stuck_at", "loc"), ("transition", "loc"), ("transition", "los")]


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


def _credit_holds(
    cfg: object, design: Design, patterns: Path, model: str, launch: str, intest: bool
) -> None:
    """Every credit reproduced by the protocol, a possible one too. And on the
    netlist with the fault tied in (acting in the load, as on the chip), each
    stuck-at credited possible is possible -- not a definite detection credited
    as a possible one, nor no detection at all -- and none left undetected is
    detected definitely."""
    assert not_reproduced(cfg, design.top, patterns, model, launch, intest) == []
    if model == "stuck_at":
        for key, kind, status in faults(cfg, ("possible", "undetected")):
            best = best_with_fault_throughout(
                cfg, design.top, patterns, key, kind, intest
            )
            if status == "possible":
                assert best == "possible", (key, kind)
            else:
                assert best != "definite", (key, kind)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("loop", r"combinational loop.*engine = x"),
        ("bus", r"(?i)tristate.*engine = x"),
    ],
)
def test_scan_check_on_the_x_engine_and_the_two_valued_refusal(
    tmp_path: Path,
    flow_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    refusal: str,
) -> None:
    from faultflow.runner import Runner
    from faultflow.runner.runner import RunnerError

    design = DESIGNS[name]
    for engine in ("x", "two_valued"):
        work = tmp_path / engine
        work.mkdir()
        monkeypatch.chdir(work)
        cfg = config(work, design, engine)
        Runner(cfg).init()
        Runner(cfg).scan()
        if engine == "x":
            Runner(cfg).scan_check()
            manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
            assert manifest["latest_check"]["status"] == "PASS"
            netlist = json.loads(Path(str(manifest["generic_json"])).read_text())
            cells = netlist["modules"][design.top]["cells"].values()
            if name == "bus":
                types = [c["type"] for c in cells]
                assert types.count("sky130_fd_sc_hd__ebufn_1") == 2
            else:
                # a cell reading its own output: the loop
                assert any(
                    set(c["connections"]["X"])
                    & {
                        b
                        for p, bits in c["connections"].items()
                        if p != "X"
                        for b in bits
                    }
                    for c in cells
                    if "X" in c["connections"]
                )
        else:
            with pytest.raises(RunnerError, match=refusal):
                Runner(cfg).scan_check()


@pytest.mark.integration
@pytest.mark.parametrize("name", list(DESIGNS))
@pytest.mark.parametrize(("model", "launch"), FLOWS)
def test_scan_credit_is_the_protocols(
    tmp_path: Path,
    flow_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    model: str,
    launch: str,
) -> None:
    """Every credit reproduced, and a fault left redundant detected by no load
    and capture at all (the designs are small enough to try every one)."""
    design = DESIGNS[name]
    monkeypatch.chdir(tmp_path)
    cfg, patterns = campaign(tmp_path, design, model, launch)
    assert faults(cfg, ("detected",))
    _credit_holds(cfg, design, patterns, model, launch, intest=False)
    assert redundant_detected(cfg, design, model, launch) == []


@pytest.mark.integration
@pytest.mark.parametrize("name", list(DESIGNS))
@pytest.mark.parametrize(("model", "launch"), FLOWS)
def test_intest_credit_is_the_protocols(
    tmp_path: Path,
    flow_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    model: str,
    launch: str,
) -> None:
    design = DESIGNS[name]
    monkeypatch.chdir(tmp_path)
    cfg, patterns = campaign(tmp_path, design, model, launch, intest=True)
    assert faults(cfg, ("detected",))
    _credit_holds(cfg, design, patterns, model, launch, intest=True)
