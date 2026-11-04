"""Unknown cells (`unsupported_cells = blackbox`): a cell missing from the cell map
has unknown outputs in every flow. Nothing its value reaches is credited, a fault
only it blocks is blackbox_unresolved (testable were the cell transparent), never
redundant, and the report names the cell.

Fixture: FOOGATE(a) -> n, y1 = n | b, y2 = n & c; the scan variant adds a flop on
d ^ b. Before, a's faults were redundant, b's credited through n (read as 0), c's
SAT tests (n free) rejected by simulation every round, and the report called the
boundary "none"."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config

ROOT = Path(__file__).resolve().parents[2]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

COMB = """(* blackbox *)
module FOOGATE(input A, output Y);
endmodule

module foo(input a, input b, input c, output y1, output y2);
  wire n;
  FOOGATE u_foo(.A(a), .Y(n));
  assign y1 = n | b;
  assign y2 = n & c;
endmodule
"""

SEQ = """(* blackbox *)
module FOOGATE(input A, output Y);
endmodule

module foo(input clk, input a, input b, input c, input d,
           output y1, output y2, output q);
  wire n;
  reg r;
  FOOGATE u_foo(.A(a), .Y(n));
  assign y1 = n | b;
  assign y2 = n & c;
  always @(posedge clk) r <= d ^ b;
  assign q = r;
endmodule
"""


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("needs yosys")


def _ofs(work: Path, rtl: str, model: str, scan: bool) -> Path:
    (work / "foo.v").write_text(rtl, encoding="utf-8")
    ofs = work / "foo.ofs"
    text = (
        f"[design]\nnetlist = {work / 'foo.v'}\ncell_lib = {CELL_MAP}\n"
        f"liberty = {LIBERTY}\n\n"
        f"[fault_model]\nmodel = {model}\ncollapsing = false\n\n"
        "[simulation]\nunsupported_cells = blackbox\nverify = false\n\n"
        "[atpg]\nrandom_vectors = 8\nmax_rounds = 3\ncompaction = none\n"
    )
    if scan:
        text += "\n[scan]\nchains = 1\n"
    ofs.write_text(text, encoding="utf-8")
    return ofs


def _flow(
    work: Path, monkeypatch: pytest.MonkeyPatch, *, model: str, scan: bool
) -> Any:
    from faultflow.cli import main

    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(work)
    ofs = _ofs(work, SEQ if scan else COMB, model, scan)
    steps = (
        [["init"], ["scan"], ["scan-check"], ["sim", "--scan"]]
        if scan
        else [["init"], ["sim"]]
    )
    for step in steps:
        assert main([*step, "--top", "foo", "-c", str(ofs)]) == 0, step
    return load_config(ofs, "foo")


def _classes(cfg: Any) -> dict[tuple[str, str], set[str]]:
    """(net name, fault type) -> the classes its faults got: detected,
    redundant, blackbox_unresolved, undetected or excluded_<why>."""
    conn = sqlite3.connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT net_name, fault_type, status, exclusion, blackbox_unresolved "
            "FROM faults WHERE campaign_id = (SELECT MAX(id) FROM campaigns)"
        ).fetchall()
    finally:
        conn.close()
    out: dict[tuple[str, str], set[str]] = {}
    for name, ftype, status, exclusion, bbu in rows:
        if exclusion != "none":
            cls = f"excluded_{exclusion}"
        elif bbu:
            cls = "blackbox_unresolved"
        else:
            cls = str(status)
        out.setdefault((str(name), str(ftype)), set()).add(cls)
    return out


def _report(cfg: Any) -> dict[str, Any]:
    path = cfg.intermediate_dir / "coverage_report.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("model", ["stuck_at", "transition"])
def test_combinational_unknown_cell_blocks_are_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None, model: str
) -> None:
    cfg = _flow(tmp_path, monkeypatch, model=model, scan=False)
    classes = _classes(cfg)
    # Every design input and output sits behind (or in front of) the unknown
    # cell: none credited, none redundant, none left to retry.
    for net in ("a", "b", "c", "y1", "y2"):
        for ftype in ("sa0", "sa1"):
            assert classes[(net, ftype)] == {"blackbox_unresolved"}, (net, ftype)
    # The cell's output net: excluded, as Policy 1 has it.
    assert all(
        c == {"excluded_blackbox"} for (net, _), c in classes.items() if net == "n"
    )
    report = _report(cfg)
    assert report["policy"]["unknown_cells"] == ["u_foo"]
    assert report["policy"]["blackbox_boundary"] == "opaque"
    assert report["summary"].get("redundant", 0) == 0
    assert report["summary"]["blackbox_unresolved"] == 10
    assert "unknown_cells:       u_foo" in (cfg.output_dir / "coverage.rpt").read_text(
        encoding="utf-8"
    )


def test_scan_unknown_cell_is_masked_and_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    cfg = _flow(tmp_path, monkeypatch, model="stuck_at", scan=True)
    classes = _classes(cfg)
    for net in ("a", "c", "y1", "y2"):
        for ftype in ("sa0", "sa1"):
            assert classes[(net, ftype)] == {"blackbox_unresolved"}, (net, ftype)
    # b fans out to the unknown cell's logic and to the scanned flop: the flop
    # side is tested, the other is not.
    for ftype in ("sa0", "sa1"):
        assert classes[("b", ftype)] == {"detected", "blackbox_unresolved"}
        assert classes[("d", ftype)] == {"detected"}
        assert classes[("q", ftype)] == {"detected"}
    report = _report(cfg)
    assert report["policy"]["unknown_cells"] == ["u_foo"]
    assert report["summary"].get("redundant", 0) == 0


def test_unknown_cell_model_is_in_the_config_fingerprint(tmp_path: Path) -> None:
    """Under `blackbox` the unknown-cell model is part of the config
    fingerprint, so a campaign graded the old way (n read as 0) does not resume
    into one graded now; under `fail` the config_hash stays as it was."""
    from faultflow.runner.runner import Runner

    ofs = _ofs(tmp_path, COMB, "stuck_at", scan=False)
    blackbox = Runner(load_config(ofs, "foo"))._config_fingerprint_payload()
    assert blackbox["unknown_cells"] == "x"
    ofs.write_text(
        ofs.read_text(encoding="utf-8").replace(
            "unsupported_cells = blackbox", "unsupported_cells = fail"
        ),
        encoding="utf-8",
    )
    assert (
        "unknown_cells"
        not in Runner(load_config(ofs, "foo"))._config_fingerprint_payload()
    )
