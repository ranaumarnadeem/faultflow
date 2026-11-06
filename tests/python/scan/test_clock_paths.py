"""Clocks through gates (faultflow.scan.clock_gates.clock_path): a flop's clock pin
followed back through buffers, through clock gates, and through a mux or an AND/OR
that ``[scan] hold`` keeps passing the clock on, to the input port that pulses it --
its root clock, which scan stitching groups it by and the manifest records with the
path. A flop whose clock reaches no port that way, or reaches it inverted, stays out
of scan with its reason. Every net on a clock path is a clock net (Policy 3)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from scan_credit import credit_not_reproduced
from scan_replay import replay_on_cells

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
HD = "sky130_fd_sc_hd"

# Four flops: two on c2, one on c1, one on clk itself. Each fixture below builds c1
# and c2 from clk its own way.
TEMPLATE = """module ck (input clk, input a, input b, {inputs}output y);
  reg r0, r1, r2, r3;
  wire c1, c2;
{path}
  always @(posedge c2) begin
    r0 <= a ^ r1;
    r1 <= r0 & b;
  end
  always @(posedge c1) r2 <= r0 | r3;
  always @(posedge clk) r3 <= r2 ^ a;
  assign y = r1 ^ r2 ^ r3;
endmodule
"""

STRAIGHT = ("", "  assign c1 = clk;\n  assign c2 = clk;")
BUFFERS = (
    "",
    f"  {HD}__clkbuf_1 u_b1 (.A(clk), .X(c1));\n"
    f"  {HD}__clkbuf_2 u_b2 (.A(c1), .X(c2));",
)
# A test-mode clock mux: [scan] hold tm:0 selects clk.
MUX = (
    "input pll, input tm, ",
    f"  {HD}__mux2_1 u_m (.A0(clk), .A1(pll), .S(tm), .X(c1));\n"
    f"  {HD}__clkbuf_1 u_b2 (.A(c1), .X(c2));",
)
# A clock ANDed with an enable [scan] hold keeps at 1.
AND = (
    "input en, ",
    f"  {HD}__and2_1 u_a (.A(clk), .B(en), .X(c1));\n"
    f"  {HD}__clkbuf_1 u_b2 (.A(c1), .X(c2));",
)
INVERTED = (
    "",
    f"  {HD}__clkinv_1 u_i (.A(clk), .Y(c1));\n"
    f"  {HD}__clkbuf_1 u_b2 (.A(c1), .X(c2));",
)
# A buffer ahead of a clock gate, and one behind it.
GATED = (
    "",
    f"  wire g;\n"
    f"  {HD}__clkbuf_1 u_b1 (.A(clk), .X(c1));\n"
    f"  {HD}__dlclkp_1 u_cg (.CLK(c1), .GATE(b), .GCLK(g));\n"
    f"  {HD}__clkbuf_1 u_b2 (.A(g), .X(c2));",
)
# A clock divided by a flop: no port pulses it.
DIVIDED = (
    "",
    "  reg div;\n  always @(posedge clk) div <= ~div;\n"
    "  assign c1 = div;\n  assign c2 = div;",
)


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    for tool in ("yosys", "iverilog", "vvp"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")


def _ofs(
    work: Path, fixture: tuple[str, str], sections: str = "", chains: int = 1
) -> Path:
    rtl = work / "ck.v"
    rtl.write_text(
        TEMPLATE.format(inputs=fixture[0], path=fixture[1]), encoding="utf-8"
    )
    ofs = work / "ck.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {rtl}\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        f"[scan]\nchains = {chains}\n{sections}",
        encoding="utf-8",
    )
    return ofs


def _flow(
    work: Path,
    fixture: tuple[str, str],
    sections: str = "",
    monkeypatch: pytest.MonkeyPatch | None = None,
    full: bool = True,
) -> tuple[Any, Path, Path]:
    """init, scan and -- ``full`` -- scan-check and sim --scan, its patterns
    exported, in ``work`` (the working directory: each flow its own output/)."""
    from faultflow.cli import main

    work.mkdir(parents=True, exist_ok=True)
    if monkeypatch is not None:
        monkeypatch.chdir(work)
    ofs = _ofs(work, fixture, sections)
    patterns = work / "patterns.json"
    steps: list[list[str]] = [["init"], ["scan"]]
    if full:
        steps += [["scan-check"], ["sim", "--scan", "--export-patterns", str(patterns)]]
    for step in steps:
        assert main([*step, "--top", "ck", "-c", str(ofs)]) == 0, step
    return load_config(ofs, "ck"), ofs, patterns


def _manifest(cfg: Any) -> dict[str, Any]:
    return dict(json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8")))


def _faults(cfg: Any) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT fault_site_key, status, exclusion FROM faults "
            "WHERE campaign_id = (SELECT MAX(id) FROM campaigns)"
        ).fetchall()
    finally:
        conn.close()
    return [(str(k), str(s), str(e)) for k, s, e in rows]


def _net_of(cfg: Any, name: str) -> int:
    manifest = _manifest(cfg)
    module = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    (net,) = module["modules"]["ck"]["netnames"][name]["bits"]
    return int(net)


def _ineligible(manifest: dict[str, Any]) -> dict[str, str]:
    return {str(ff["instance"]): str(ff["reason"]) for ff in manifest["ineligible_ffs"]}


@pytest.mark.integration
def test_flops_behind_clock_buffers_scan_on_the_port_as_one_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    cfg, ofs, patterns = _flow(tmp_path / "buf", BUFFERS, monkeypatch=monkeypatch)
    manifest = _manifest(cfg)
    clk = _net_of(cfg, "clk")
    # One root clock, the port: four flops on the one chain, none left out.
    assert manifest["cell_count"] == 4 and not manifest["ineligible_ffs"]
    assert manifest["clock_nets"] == [clk]
    assert manifest["clock_ports"] == ["clk"]
    # Each flop's pin stays on its buffer.
    pins = {
        c["instance"]: c.get("pin_clock_net", c["clock_net"]) for c in manifest["cells"]
    }
    assert set(pins.values()) == {clk, _net_of(cfg, "c1"), _net_of(cfg, "c2")}

    # The clock tree's nets are clock nets: every fault on them is excluded.
    faults = _faults(cfg)
    tree = {_net_of(cfg, "c1"), _net_of(cfg, "c2"), clk}
    on_tree = [f for f in faults if int(f[0].split(":")[1]) in tree]
    assert on_tree and {f[2] for f in on_tree} == {"clock"}, on_tree

    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns) == []

    # The same counts as the design clocked straight from the port: only the
    # buffers' nets, clock nets, are added.
    straight, _, _ = _flow(tmp_path / "straight", STRAIGHT, monkeypatch=monkeypatch)
    graded = Counter((s, e) for _, s, e in faults if e != "clock")
    graded_straight = Counter((s, e) for _, s, e in _faults(straight) if e != "clock")
    assert graded == graded_straight


@pytest.mark.integration
def test_flops_behind_clock_buffers_transition_patterns_hold_in_the_full_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(
        tmp_path,
        BUFFERS,
        "\n[fault_model]\nmodel = transition\nlaunch = loc\ncollapsing = false\n",
    )
    assert json.loads(patterns.read_text(encoding="utf-8"))
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns, loc=True) == []


@pytest.mark.integration
def test_flops_behind_clock_buffers_compress_compact_and_write_stil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    ofs = _ofs(
        tmp_path,
        BUFFERS,
        "\n[compression]\nenabled = true\nchannels = 4\n\n"
        "[compaction]\nenabled = true\nchannels = 1\n",
        chains=2,
    )
    patterns = tmp_path / "patterns.json"
    for step in (
        ["init"],
        ["scan"],
        ["scan-check"],
        ["scan-compress"],
        ["scan-compact"],
        ["sim", "--scan", "--export-patterns", str(patterns)],
        [
            "write-patterns",
            "--patterns",
            str(patterns),
            "-o",
            str(tmp_path / "ck.stil"),
        ],
    ):
        assert main([*step, "--top", "ck", "-c", str(ofs)]) == 0, step
    assert (tmp_path / "ck.stil").read_text(encoding="utf-8").startswith("STIL")
    cfg = load_config(ofs, "ck")
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "fixture,hold,name",
    [(MUX, "tm:0", "mux"), (AND, "en:1", "and")],
    ids=["mux", "and"],
)
def test_a_clock_through_a_gate_a_hold_keeps_open_scans_on_the_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    fixture: tuple[str, str],
    hold: str,
    name: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(tmp_path, fixture, f"hold = {hold}\n")
    manifest = _manifest(cfg)
    assert manifest["cell_count"] == 4 and not manifest["ineligible_ffs"]
    assert manifest["clock_ports"] == ["clk"]
    (path,) = manifest["clock_paths"].values()
    held_port = hold.split(":")[0]
    assert {h["port"] for h in path["holds"]} == {held_port}

    # The held side input is no clock: its faults are graded.
    faults = _faults(cfg)
    held = _net_of(cfg, held_port)
    assert {e for k, _, e in faults if int(k.split(":")[1]) == held} == {"none"}
    c1 = _net_of(cfg, "c1")
    assert {e for k, _, e in faults if int(k.split(":")[1]) == c1} == {"clock"}

    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns) == []


@pytest.mark.integration
def test_a_clock_mux_nothing_holds_leaves_its_flops_out_of_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    cfg, _, _ = _flow(tmp_path, MUX, monkeypatch=monkeypatch, full=False)
    manifest = _manifest(cfg)
    reasons = _ineligible(manifest)
    assert len(reasons) == 3 and set(reasons.values()) == {"clock_not_from_port"}
    assert manifest["cell_count"] == 1 and manifest["clock_ports"] == ["clk"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "fixture,reason",
    [(INVERTED, "inverted_clock"), (DIVIDED, "clock_not_from_port")],
    ids=["inverted", "divided"],
)
def test_a_flop_whose_clock_no_port_pulses_as_is_stays_out_of_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    fixture: tuple[str, str],
    reason: str,
) -> None:
    cfg, _, _ = _flow(tmp_path, fixture, monkeypatch=monkeypatch, full=False)
    manifest = _manifest(cfg)
    reasons = _ineligible(manifest)
    assert len(reasons) == 3 and set(reasons.values()) == {reason}, reasons
    assert manifest["clock_ports"] == ["clk"]


@pytest.mark.integration
def test_a_clock_gate_between_clock_buffers_scans_on_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(tmp_path, GATED)
    manifest = _manifest(cfg)
    assert manifest["cell_count"] == 4 and not manifest["ineligible_ffs"]
    assert manifest["clock_ports"] == ["clk"]
    gated = {c["instance"] for c in manifest["cells"] if c.get("clock_gates")}
    assert len(gated) == 2
    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns) == []


@pytest.mark.integration
def test_scan_check_refuses_a_clock_path_the_holds_no_longer_keep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    ofs = _ofs(tmp_path, MUX, "hold = tm:0\n")
    for step in (["init"], ["scan"], ["scan-check"]):
        assert main([*step, "--top", "ck", "-c", str(ofs)]) == 0, step
    ofs.write_text(
        ofs.read_text(encoding="utf-8").replace("hold = tm:0", "hold = tm:1"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        main(["scan-check", "--top", "ck", "-c", str(ofs)])
    err = capsys.readouterr().err
    assert "clock" in err and "tm" in err


@pytest.mark.integration
@pytest.mark.parametrize(
    "fixture,hold,errors",
    [
        (BUFFERS, "", 0),
        (MUX, "", 2),  # c1 and c2, the mux's and its buffer's
        (MUX, "tm:0", 0),
        (INVERTED, "", 0),
        (DIVIDED, "", 1),
    ],
    ids=["buffers", "mux", "mux-held", "inverted", "divided"],
)
def test_rule_check_traces_clocks_as_scan_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    fixture: tuple[str, str],
    hold: str,
    errors: int,
) -> None:
    """CLK001 flags a clock no port pulses, through the same tracer and holds as
    scan stitching. An inverted clock is controllable (its flops just stay out of
    scan)."""
    from faultflow.cli import main

    monkeypatch.chdir(tmp_path)
    ofs = _ofs(tmp_path, fixture, f"hold = {hold}\n" if hold else "")
    for step in (["init"], ["scan"]):  # scan synthesizes the netlist
        assert main([*step, "--top", "ck", "-c", str(ofs)]) == 0
    assert main(["rule_check", "--top", "ck", "-c", str(ofs), "--advisory"]) == 0
    cfg = load_config(ofs, "ck")
    report = json.loads((cfg.output_dir / "rule_check.json").read_text("utf-8"))
    clk001 = [v for v in report["violations"] if v["rule_id"] == "CLK001"]
    assert len(clk001) == errors, clk001


def _on_net(cfg: Any, net: int) -> list[tuple[str, str, int]]:
    """(fault type, status, hold_unresolved) of each fault on ``net``'s stem."""
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT fault_type, status, hold_unresolved FROM faults "
            "WHERE campaign_id = (SELECT MAX(id) FROM campaigns) "
            "AND fault_site_key = ?",
            (f"net:{net}:stem",),
        ).fetchall()
    finally:
        conn.close()
    return [(str(t).lower(), str(s), int(h)) for t, s, h in rows]


@pytest.mark.integration
@pytest.mark.parametrize(
    "fixture,hold,side",
    [(MUX, "tm:0", ("tm", "pll")), (AND, "en:1", ("en",))],
    ids=["mux", "and"],
)
def test_a_fault_only_a_clock_path_reads_is_hold_unresolved_not_redundant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow_tools: None,
    fixture: tuple[str, str],
    hold: str,
    side: tuple[str, ...],
) -> None:
    """The scan view has no flops to read a clock path, so a held side input's
    faults and an unselected clock input's are UNSAT there. That proves nothing:
    each changes the scan clock, or would once the hold lifts. They stay in the
    denominator, never redundant, and nothing credits them."""
    from faultflow.db import connect, summary

    monkeypatch.chdir(tmp_path)
    cfg, _, patterns = _flow(tmp_path, fixture, f"hold = {hold}\n")

    for name in side:
        assert sorted(_on_net(cfg, _net_of(cfg, name))) == [
            ("sa0", "undetected", 1),
            ("sa1", "undetected", 1),
        ], name
    with connect(cfg.db_path) as conn:
        (campaign,) = conn.execute("SELECT MAX(id) FROM campaigns").fetchone()
        data = summary(conn, campaign_id=campaign)
    assert data["redundant"] == 0
    assert data["hold_unresolved"] == 2 * len(side)
    assert data["denominator"] == data["detected"] + 2 * len(side)

    assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
    assert credit_not_reproduced(cfg, patterns) == []


@pytest.mark.integration
def test_a_held_input_stays_graded_when_the_gate_it_feeds_clocks_no_flop_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    """Only u_a1's output reaches a clock pin through buffers, so the core takes
    u_a0's output for data; the manifest's clock path says it is a clock net. en0 SA0
    must not collapse into that excluded fault (with collapsing, the default): it
    stays in the denominator as it does without collapsing."""
    two_gates = (
        "input en0, input en1, ",
        "  wire c0;\n"
        f"  {HD}__and2_1 u_a0 (.A(clk), .B(en0), .X(c0));\n"
        f"  {HD}__and2_1 u_a1 (.A(c0), .B(en1), .X(c1));\n"
        f"  {HD}__clkbuf_1 u_b2 (.A(c1), .X(c2));",
    )
    from faultflow.db import connect, summary

    monkeypatch.chdir(tmp_path)
    cfg, _, _ = _flow(tmp_path, two_gates, "hold = en0:1, en1:1\n")

    for name in ("en0", "en1"):
        assert sorted(_on_net(cfg, _net_of(cfg, name))) == [
            ("sa0", "undetected", 1),
            ("sa1", "undetected", 1),
        ], name
    with connect(cfg.db_path) as conn:
        (campaign,) = conn.execute("SELECT MAX(id) FROM campaigns").fetchone()
        data = summary(conn, campaign_id=campaign)
    assert data["hold_unresolved"] == 4
    assert data["denominator"] == data["detected"] + 4


@pytest.mark.integration
def test_a_clock_enable_tied_high_needs_no_hold_and_its_stuck_low_is_no_redundancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    """A constant keeps the AND passing the clock with no [scan] hold, so the view
    models no non-scan cell. Stuck at 0 it stops the clock, which is not redundant;
    stuck at 1 it changes nothing, which is."""
    tied = (
        "",
        f"  {HD}__and2_1 u_a (.A(clk), .B(1'b1), .X(c1));\n"
        f"  {HD}__clkbuf_1 u_b2 (.A(c1), .X(c2));",
    )
    monkeypatch.chdir(tmp_path)
    cfg, _, _ = _flow(tmp_path, tied)
    assert sorted(_on_net(cfg, -2)) == [
        ("sa0", "undetected", 1),
        ("sa1", "redundant", 0),
    ]
