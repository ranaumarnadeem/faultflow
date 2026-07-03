"""The scan-techmap equivalence check must verify the ACTUAL on-disk `sky130_verilog`
artifact, not just a freshly re-derived one.

`_run_scan_techmap_equivalence_check` (runner.py) re-runs `run_scan_techmap_json` on
the SAME techmap-library file (`manifest["techmap_verilog"]`) that produced
`sky130_verilog` in the first place, and compares THAT freshly-rederived JSON against
the generic scan JSON. It never reads `manifest["sky130_verilog"]` itself -- only
checks the file exists and passes its path through as metadata. So if the real,
on-disk `sky130_verilog` (the file that would actually be handed to a physical scan
flow / iverilog verification) drifts from what re-running the techmap would produce
-- corrupted, hand-edited, from a stale/different techmap run -- the equivalence
check has no way to notice, since it never looks at that file's actual content.

`faultflow/scan/yosys.py::verilog_to_json` (import gate-level Verilog for C++ JSON
simulation) exists specifically for this: converting a real Verilog netlist into
JSON so its behavior can be compared. It had no caller anywhere in the codebase
before this fix -- this wires it in as the missing third comparison leg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.runner import Runner, RunnerError
from faultflow.scan.reports import load_manifest
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _build_techmapped_dff(tmp_path: Path) -> tuple[Runner, Path]:
    """Read/synth/scan-insert/techmap a tiny D flip-flop; return the Runner (with
    a fully populated scan manifest, including `sky130_verilog`) and the manifest
    path."""
    rtl = tmp_path / "tiny_dff.v"
    rtl.write_text(
        "module tiny_dff(input clk, input d, output q);\n"
        "  reg q_r;\n"
        "  always @(posedge clk) q_r <= d;\n"
        "  assign q = q_r;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "tiny_dff")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    bridge.call("add_scan", "-chains", "1")
    bridge.call("check_scan", "-structural")

    cfg = session.materialize_config()
    runner = Runner(cfg)
    runner.scan_techmap()

    manifest_path = cfg.scan_manifest_path
    return runner, manifest_path


@pytest.mark.integration
def test_equivalence_check_catches_stale_sky130_verilog(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A `sky130_verilog` file that disagrees with the generic scan JSON (here:
    hand-corrupted after a legitimate techmap run, simulating drift/corruption)
    must fail scan_check(require_techmap=True) -- not silently pass because the
    equivalence check only re-derives its own comparison JSON and never reads the
    real file."""
    import shutil

    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    runner, manifest_path = _build_techmapped_dff(tmp_path)
    manifest = load_manifest(manifest_path)
    sky130_v = Path(str(manifest["sky130_verilog"]))
    assert sky130_v.exists()

    # Corrupt the on-disk artifact: invert Q so its behavior genuinely differs from
    # the generic scan JSON, while leaving techmap_verilog (the mapping rules file
    # the equivalence check re-derives its comparison from) untouched.
    corrupted = sky130_v.read_text(encoding="utf-8").replace(
        "assign q = q_r;", "assign q = ~q_r;"
    )
    # If the netlist doesn't literally contain that assignment (Yosys emits gate
    # instances, not behavioral assigns), corrupt structurally instead: flip the
    # top-level q port's driver by renaming it so no cell drives the real `q`.
    if corrupted == sky130_v.read_text(encoding="utf-8"):
        text = sky130_v.read_text(encoding="utf-8")
        corrupted = text.replace(".Q(q)", ".Q(q_unused)", 1)
        assert corrupted != text, "could not construct a corrupting edit"
    sky130_v.write_text(corrupted, encoding="utf-8")

    with pytest.raises(RunnerError):
        runner.scan_check(require_techmap=True)


@pytest.mark.integration
def test_equivalence_check_passes_for_a_genuine_techmap(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """Positive-path regression: an untouched, freshly-techmapped sky130_verilog
    must still pass -- the new third comparison leg must not be a false positive."""
    import shutil

    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")

    runner, _manifest_path = _build_techmapped_dff(tmp_path)

    result = runner.scan_check(require_techmap=True)

    assert "check=PASS" in result or "PASS" in result
