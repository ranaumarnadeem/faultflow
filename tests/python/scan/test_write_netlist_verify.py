"""`write_netlist -scan -techmap -verify` must prove the written techmapped netlist
behaves identically to the generic scan design it was mapped from.

`write_netlist -verify` was a hard "techmap verification is not implemented" stub.
It now fault-free sequence-simulates the just-written Sky130 scan netlist against the
generic `$scanff_faultflow` design over the scan vectors and fails at write time if
their outputs differ -- catching a broken or drifted physical-cell binding before the
netlist is ever handed downstream.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.runner import Runner, RunnerError
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _scanned_session(tmp_path: Path) -> tuple[ProjectSession, TclBridge]:
    """Read/synth/scan-insert a tiny D flip-flop; the session is left scan-inserted
    with a scan manifest on disk (what write_netlist -scan needs)."""
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
    return session, bridge


# --------------------------------------------------------------------------- #
# Precondition: -verify requires -scan -techmap (no yosys/core needed)         #
# --------------------------------------------------------------------------- #


def test_verify_without_scan_or_techmap_errors(tmp_path: Path) -> None:
    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)

    with pytest.raises(ShellError) as exc:
        bridge.call("write_netlist", "-verify")

    assert exc.value.code == ("FAULTFLOW", "PRECONDITION", "VERIFY_REQUIRES_TECHMAP")


def test_verify_with_scan_but_no_techmap_errors(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    _session, bridge = _scanned_session(tmp_path)

    with pytest.raises(ShellError) as exc:
        bridge.call("write_netlist", "-scan", "-verify")

    assert exc.value.code == ("FAULTFLOW", "PRECONDITION", "VERIFY_REQUIRES_TECHMAP")


# --------------------------------------------------------------------------- #
# End-to-end: genuine techmap passes, corrupted mapping fails                  #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_write_netlist_verify_passes_for_genuine_techmap(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    _session, bridge = _scanned_session(tmp_path)

    result = bridge.call("write_netlist", "-scan", "-techmap", "-verify")
    text = str(result)

    assert "verify PASS" in text
    # The written netlist is a real Sky130 scan cell netlist, not the generic one.
    written = tmp_path / "out" / "tiny_dff" / "tiny_dff_scan.v"
    assert written.exists()
    assert "sky130_fd_sc_hd__sdfxtp_1" in written.read_text(encoding="utf-8")


@pytest.mark.integration
def test_verify_techmapped_netlist_catches_broken_mapping(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """The verifier must FAIL when the on-disk mapped netlist disagrees with the
    generic scan design -- here a genuine techmap is written, then its output pin
    is structurally rerouted so no cell drives the real `q`."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    session, bridge = _scanned_session(tmp_path)

    # A clean techmap+verify passes first (sanity: the verifier is not vacuous).
    bridge.call("write_netlist", "-scan", "-techmap", "-verify")

    written = tmp_path / "out" / "tiny_dff" / "tiny_dff_scan.v"
    text = written.read_text(encoding="utf-8")
    # Reroute the flop's Q so the top-level output `q` is no longer driven by the
    # captured value -- a genuine functional divergence from the generic design.
    corrupted = text.replace(".Q(q)", ".Q(\\q_unused )", 1)
    if corrupted == text:
        corrupted = text.replace(".Q(q_r)", ".Q(\\q_unused )", 1)
    assert corrupted != text, "could not construct a corrupting edit"
    written.write_text(corrupted, encoding="utf-8")

    runner = Runner(session.materialize_config())
    with pytest.raises(RunnerError, match="outputs differ from the generic"):
        runner.verify_techmapped_netlist(written)
