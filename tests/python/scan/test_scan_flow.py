from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from faultflow.cli import main
from faultflow.scan import (
    ScanError,
    YOSYS_SCAN_CELL_TYPE,
    balanced_chain_lengths,
    render_scan_techmap,
    run_scan_techmap,
    scan_shift_capture_shiftout_cycles,
    stitch_scan_json,
    write_scan_techmap,
)

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


def _tiny_dff_json() -> dict[str, object]:
    return {
        "modules": {
            "tiny_dff": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [4]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "Q": [4]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [4], "attributes": {}},
                },
            }
        }
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_config(path: Path, netlist: Path) -> Path:
    path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
output = missing.test
""".strip() + "\n",
        encoding="utf-8",
    )
    return path


def test_stitch_scan_json_replaces_plain_ff(tmp_path: Path) -> None:
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    output = tmp_path / "tiny_dff_scan_generic.json"

    result = stitch_scan_json(source, CELL_MAP, "tiny_dff", output)

    data = json.loads(output.read_text(encoding="utf-8"))
    module = data["modules"]["tiny_dff"]
    cell = module["cells"]["u0"]
    assert result.cell_count == 1
    assert result.scan_inputs == ["scan_in"]
    assert result.scan_outputs == ["scan_out"]
    assert cell["type"] == YOSYS_SCAN_CELL_TYPE
    assert cell["connections"]["SDI"] == module["ports"]["scan_in"]["bits"]
    assert cell["connections"]["SE"] == module["ports"]["scan_en"]["bits"]
    assert module["ports"]["scan_out"]["bits"] == [4]


def test_stitch_scan_json_builds_balanced_multi_chains(tmp_path: Path) -> None:
    data = _tiny_dff_json()
    modules = data["modules"]
    assert isinstance(modules, dict)
    tiny = modules["tiny_dff"]
    assert isinstance(tiny, dict)
    cells_obj = tiny["cells"]
    netnames_obj = tiny["netnames"]
    assert isinstance(cells_obj, dict)
    assert isinstance(netnames_obj, dict)
    cells: dict[str, Any] = cells_obj
    netnames: dict[str, Any] = netnames_obj
    for index in range(1, 5):
        d_net = 10 + index
        q_net = 20 + index
        cells[f"u{index}"] = {
            "hide_name": 0,
            "type": "sky130_fd_sc_hd__dfxtp_1",
            "parameters": {},
            "attributes": {},
            "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
            "connections": {"CLK": [2], "D": [d_net], "Q": [q_net]},
        }
        netnames[f"D{index}"] = {"hide_name": 0, "bits": [d_net], "attributes": {}}
        netnames[f"Q{index}"] = {"hide_name": 0, "bits": [q_net], "attributes": {}}
    source = _write_json(tmp_path / "many_dff.json", data)
    output = tmp_path / "many_dff_scan_generic.json"

    result = stitch_scan_json(
        source,
        CELL_MAP,
        "tiny_dff",
        output,
        scan_chains=2,
        max_chain_length=3,
        scan_in_base="scan_si",
        scan_out_base="scan_so",
        scan_enable="test_se",
    )

    assert balanced_chain_lengths(5, 2) == [3, 2]
    assert result.scan_inputs == ["scan_si_0", "scan_si_1"]
    assert result.scan_outputs == ["scan_so_0", "scan_so_1"]
    assert [chain.length for chain in result.chains] == [3, 2]
    assert result.scan_enable == "test_se"


def test_stitch_scan_json_rejects_reset_ff(tmp_path: Path) -> None:
    # dffsr = async reset AND set (dfbbp): no sky130 scan cell exists, still rejected.
    source = ROOT / "tests/cpp/fixtures/tiny_dffsr.json"

    with pytest.raises(ScanError, match="has_async_reset_set"):
        stitch_scan_json(
            source,
            CELL_MAP,
            "tiny_dffsr",
            tmp_path / "tiny_dffsr_scan_generic.json",
        )


def _tiny_dfrtp_json() -> dict[str, object]:
    return {
        "modules": {
            "tiny_dfrtp": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "RESET_B": {"direction": "input", "bits": [4]},
                    "Q": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__dfrtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "RESET_B": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "RESET_B": [4], "Q": [5]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "RESET_B": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [5], "attributes": {}},
                },
            }
        }
    }


def test_stitch_scan_json_keeps_async_reset(tmp_path: Path) -> None:
    # Single async-reset FF (dfrtp) is now scannable: stitched to the reset-
    # preserving scan cell, with RESET_B wired to the original reset net.
    from faultflow.scan.stitch import YOSYS_SCAN_RESET_CELL_TYPE

    source = _write_json(tmp_path / "tiny_dfrtp.json", _tiny_dfrtp_json())
    output = tmp_path / "tiny_dfrtp_scan_generic.json"

    result = stitch_scan_json(source, CELL_MAP, "tiny_dfrtp", output)

    assert result.cell_count == 1
    assert result.ineligible_ffs == []
    data = json.loads(output.read_text(encoding="utf-8"))
    cell = data["modules"]["tiny_dfrtp"]["cells"]["u0"]
    assert cell["type"] == YOSYS_SCAN_RESET_CELL_TYPE
    assert cell["connections"]["RESET_B"] == [4]
    assert cell["connections"]["SDI"] == module_scan_in_bits(data, "tiny_dfrtp")


def module_scan_in_bits(data: dict[str, Any], top: str) -> list[int]:
    return data["modules"][top]["ports"]["scan_in"]["bits"]


def _tiny_edfxtp_json() -> dict[str, object]:
    return {
        "modules": {
            "tiny_edfxtp": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "DE": {"direction": "input", "bits": [4]},
                    "Q": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__edfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "DE": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "DE": [4], "Q": [5]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "DE": {"hide_name": 0, "bits": [4], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [5], "attributes": {}},
                },
            }
        }
    }


@pytest.mark.golden
def test_stitch_scan_json_preserves_enable_hold_behavior(
    tmp_path: Path, require_cpp_core: None
) -> None:
    """A scan-inserted edfxtp (enable D-FF) must still honor DE in normal
    (scan_en=0) mode: DE=0 holds Q, DE=1 loads D. The generic scan cell
    ($scanff_faultflow) has no DE pin at all -- only CLK/D/SDI/SE/Q (see
    cells/sky130/sky130_fd_sc_hd.json) -- so stitching must synthesize a
    hold-mux ahead of its D pin. Silently dropping DE turns every
    scan-inserted enable FF into a plain D-FF, which scan-check's
    normal-mode equivalence catches on any design with edfxtp cells
    (confirmed on picorv32a/boxcar in real sweeps)."""
    import _faultflow_core as core  # type: ignore[import-not-found]

    source = _write_json(tmp_path / "tiny_edfxtp.json", _tiny_edfxtp_json())
    output = tmp_path / "tiny_edfxtp_scan_generic.json"
    stitch_scan_json(source, CELL_MAP, "tiny_edfxtp", output)

    def pulse(d: bool, de: bool) -> list[dict[str, object]]:
        return [
            {"CLK": False, "D": d, "DE": de, "scan_en": False, "scan_in": False},
            {"CLK": True, "D": d, "DE": de, "scan_en": False, "scan_in": False},
        ]

    load_only = pulse(True, True)
    load_then_hold = pulse(True, True) + pulse(False, False)

    results = core.fault_free_sequence_outputs(
        str(output),
        str(CELL_MAP),
        [load_only, load_then_hold],
        ["CLK", "D", "DE", "scan_en", "scan_in"],
        ["Q"],
        "fail",
    )

    assert results[0] == {"Q": True}, "load pulse (DE=1) must set Q=D"
    assert results[1] == {
        "Q": True
    }, "hold pulse (DE=0) must preserve Q -- DE dropped during scan stitching"


def _tiny_edfxtp_const_de_json(const: str) -> dict[str, object]:
    return {
        "modules": {
            "tiny_edfxtp_const": {
                "attributes": {"top": "1"},
                "ports": {
                    "CLK": {"direction": "input", "bits": [2]},
                    "D": {"direction": "input", "bits": [3]},
                    "Q": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "u0": {
                        "hide_name": 0,
                        "type": "sky130_fd_sc_hd__edfxtp_1",
                        "parameters": {},
                        "attributes": {},
                        "port_directions": {
                            "CLK": "input",
                            "D": "input",
                            "DE": "input",
                            "Q": "output",
                        },
                        "connections": {"CLK": [2], "D": [3], "DE": [const], "Q": [5]},
                    }
                },
                "netnames": {
                    "CLK": {"hide_name": 0, "bits": [2], "attributes": {}},
                    "D": {"hide_name": 0, "bits": [3], "attributes": {}},
                    "Q": {"hide_name": 0, "bits": [5], "attributes": {}},
                },
            }
        }
    }


def test_stitch_scan_json_eligible_with_de_tied_to_constant_0(tmp_path: Path) -> None:
    """Yosys can tie an edfxtp's DE to a constant bit ("0"/"1" string literals,
    not a real net) when synthesis proves one bit of a wider enable-gated
    register is never/always written (observed on picorv32a as
    $auto$ff.cc:337:slice$NNNN instances). A constant is not a resolvable
    single-bit net, so it must not be treated the same as a malformed/missing
    enable pin: rejecting it makes the FF ineligible, and sim --scan requires
    FULL scan (every FF must be scannable), so a single such FF hard-blocks
    the whole design. DE tied to constant 0 (permanently disabled, given
    edfxtp's level=HIGH) means the FF holds forever -- stitching must wire
    the scan cell's D pin straight to its own Q (no mux needed, no enable
    net exists to mux on)."""
    source = _write_json(
        tmp_path / "tiny_edfxtp_const0.json", _tiny_edfxtp_const_de_json("0")
    )
    output = tmp_path / "tiny_edfxtp_const0_scan_generic.json"

    result = stitch_scan_json(source, CELL_MAP, "tiny_edfxtp_const", output)

    assert result.ineligible_ffs == []
    assert result.cell_count == 1
    data = json.loads(output.read_text(encoding="utf-8"))
    cell = data["modules"]["tiny_edfxtp_const"]["cells"]["u0"]
    assert cell["connections"]["D"] == cell["connections"]["Q"], (
        "DE tied to constant 0 must hold forever: D must be wired to the "
        "FF's own Q (self-loop hold), not left at the original data net"
    )


def test_stitch_scan_json_eligible_with_de_tied_to_constant_1(tmp_path: Path) -> None:
    """DE tied to constant 1 (permanently enabled, given edfxtp's level=HIGH)
    means the FF always loads D -- functionally a plain D-FF. Stitching must
    wire D straight through to the original data net, matching the no-enable
    case exactly (no mux synthesized for a compile-time-constant enable)."""
    source = _write_json(
        tmp_path / "tiny_edfxtp_const1.json", _tiny_edfxtp_const_de_json("1")
    )
    output = tmp_path / "tiny_edfxtp_const1_scan_generic.json"

    result = stitch_scan_json(source, CELL_MAP, "tiny_edfxtp_const", output)

    assert result.ineligible_ffs == []
    assert result.cell_count == 1
    data = json.loads(output.read_text(encoding="utf-8"))
    cell = data["modules"]["tiny_edfxtp_const"]["cells"]["u0"]
    assert cell["connections"]["D"] == [3], (
        "DE tied to constant 1 must always load: D must be wired straight "
        "to the original data net, like a plain (no-enable) FF"
    )


def test_async_reset_scan_atpg_end_to_end(tmp_path: Path) -> None:
    # The autoMBIST scenario: an async-reset controller (always_ff @(posedge clk
    # or negedge rst_n)) must scan-insert and grade. The reset is held inactive
    # during the scan test so the reduced view stays consistent with the protocol.
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "arst_seq.v"
    rtl.write_text(
        "module arst_seq (input clk, input rst_n, input a, input b,\n"
        "                 output reg dout);\n"
        "  reg q0;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) q0 <= 1'b0; else q0 <= a & b;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) dout <= 1'b0; else dout <= ~q0;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "arst_seq")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("synth")
    # Both async-reset (dfrtp) FFs must be eligible and stitched.
    assert "cells=2" in str(bridge.call("add_scan", "-chains", "1"))
    bridge.call("check_scan")  # must not raise
    bridge.call("run_atpg", "-scan", "-target", "100.0")

    report = json.loads(
        (
            tmp_path / "out/arst_seq/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    summary = report["summary"]
    assert summary["detected"] > 0
    # The combinational logic of the controller is fully gradeable.
    assert summary["undetected"] == 0


def test_async_reset_tree_faults_detected_via_implication(tmp_path: Path) -> None:
    # With include_reset_faults, the reduced ATPG view models the async control
    # (PPO = reset_active ? 0 : D), so the SAT can justify+propagate reset-tree
    # faults to a scan-observable PPO (implication-based detection, like Tessent).
    # The hold-reset-inactive shortcut is dropped in this mode.
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "arst_seq.v"
    rtl.write_text(
        "module arst_seq (input clk, input rst_n, input a, input b,\n"
        "                 output reg dout);\n"
        "  reg q0;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) q0 <= 1'b0; else q0 <= a & b;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) dout <= 1'b0; else dout <= ~q0;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "arst_seq")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    # Opt into grading the reset tree before fault enumeration.
    bridge.call("set_option", "fault_model.include_reset_faults", "true")
    bridge.call("synth")
    assert "cells=2" in str(bridge.call("add_scan", "-chains", "1"))
    bridge.call("check_scan")
    bridge.call("run_atpg", "-scan", "-target", "100.0")

    report = json.loads(
        (
            tmp_path / "out/arst_seq/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    summary = report["summary"]
    # Reset-tree faults are now in the denominator (not excluded)...
    assert summary["excluded_reset"] == 0
    # ...and every fault, including the reset tree, is detected.
    assert summary["undetected"] == 0
    assert summary["detected"] > 0


@pytest.mark.parametrize("launch", ["broadside", "los"])
def test_async_reset_transition_atpg_with_reset_faults(
    tmp_path: Path, launch: str
) -> None:
    # Two-frame (transition) counterpart of the implication test: with the reset
    # modeled on BOTH FF paths and across BOTH frames, the LOC/LOS golden gate
    # must still hold (run_atpg must not raise golden_sequence_failed) while the
    # reset tree is graded rather than excluded.
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    rtl = tmp_path / "arst_seq.v"
    rtl.write_text(
        "module arst_seq (input clk, input rst_n, input a, input b,\n"
        "                 output reg dout);\n"
        "  reg q0;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) q0 <= 1'b0; else q0 <= a & b;\n"
        "  always @(posedge clk or negedge rst_n)\n"
        "    if (!rst_n) dout <= 1'b0; else dout <= ~q0;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    session = ProjectSession(output_root=tmp_path / "out")
    bridge = TclBridge(session)
    bridge.call("read_netlist", str(rtl), "-top", "arst_seq")
    bridge.call("use_lib_cells", "sky130")
    bridge.call("add_clock", "clk")
    bridge.call("set_option", "fault_model.include_reset_faults", "true")
    bridge.call("set_option", "fault_model.collapsing", "false")
    # buffer WBC: transition (LOC/LOS) ATPG is unsupported with the 1-FF scan WBC.
    bridge.call("set_option", "wrap.wbr_model", "buffer")
    bridge.call("synth")
    assert "cells=2" in str(bridge.call("add_scan", "-chains", "1"))
    bridge.call("check_scan")
    # Must not raise: validates the two-frame golden gate with reset modeled.
    bridge.call("run_atpg", "-scan", "-tf", launch, "-target", "100.0")

    report = json.loads(
        (
            tmp_path / "out/arst_seq/.faultflow/intermediate/coverage_report.json"
        ).read_text(encoding="utf-8")
    )
    summary = report["summary"]
    # Reset-tree transition faults are graded (in the denominator), not excluded.
    assert summary["excluded_reset"] == 0
    assert summary["detected"] > 0


def test_render_scan_techmap_targets_sky130_scan_cell() -> None:
    text = render_scan_techmap()
    celltype = YOSYS_SCAN_CELL_TYPE.replace("\\", "\\\\")

    assert f'techmap_celltype = "{celltype}"' in text
    assert "sky130_fd_sc_hd__sdfxtp_1 _TECHMAP_REPLACE_" in text
    assert ".SCD(SDI)" in text
    assert ".SCE(SE)" in text


def test_scan_protocol_shift_capture_shiftout_cycles() -> None:
    cycles = scan_shift_capture_shiftout_cycles(
        [True, False],
        {"D": True},
        shift_out_length=2,
    )

    assert len(cycles) == 10
    assert cycles[0]["CLK"] is False
    assert cycles[1]["CLK"] is True
    assert cycles[1]["scan_en"] is True
    assert cycles[1]["scan_in"] is True
    assert cycles[5]["scan_en"] is False


def test_scan_cli_writes_manifest_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0

    out = tmp_path / "output/tiny_dff"
    assert (out / "tiny_dff_scan.json").exists()
    workspace = out / ".faultflow"
    assert (workspace / "generated_scripts" / "faultflow_scanff_map.v").exists()
    manifest = json.loads((workspace / "manifests" / "scan_manifest.json").read_text())
    assert manifest["cell_count"] == 1
    assert (workspace / "manifests" / "scan_chains.txt").exists()
    assert (out / "scan.rpt").exists()
    assert manifest["sky130_verilog"] is None


def test_scan_cli_dry_run_writes_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert (
        main(
            [
                "scan",
                "--top",
                "tiny_dff",
                "-c",
                str(cfg),
                "--dry-run",
                "--scan-chains",
                "1",
            ]
        )
        == 0
    )

    text = capsys.readouterr().out
    assert "scan dry-run top=tiny_dff" in text
    assert "eligible_ffs=1" in text
    assert not (
        tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json"
    ).exists()


def test_scan_check_writes_fail_result_on_structural_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0
    generic = tmp_path / "output/tiny_dff/tiny_dff_scan.json"
    generic.write_text(generic.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["scan-check", "--top", "tiny_dff", "-c", str(cfg)])

    assert exc.value.code == 2
    manifest = json.loads(
        (
            tmp_path / "output/tiny_dff/.faultflow/manifests/scan_manifest.json"
        ).read_text()
    )
    assert manifest["latest_check"]["status"] == "FAIL"
    assert "generic JSON hash" in manifest["latest_check"]["errors"][0]


@pytest.mark.integration
def test_yosys_scan_techmap_produces_sky130_cell(tmp_path: Path) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    generic = tmp_path / "tiny_dff_scan_generic.json"
    techmap = tmp_path / "faultflow_scanff_map.v"
    out_v = tmp_path / "tiny_dff_scan.v"
    stitch_scan_json(source, CELL_MAP, "tiny_dff", generic)
    write_scan_techmap(techmap)

    run_scan_techmap(
        generic_json=generic,
        techmap_verilog=techmap,
        output_verilog=out_v,
        top="tiny_dff",
        log_path=tmp_path / "yosys_scan.log",
        script_path=tmp_path / "yosys_scan.ys",
    )

    text = out_v.read_text(encoding="utf-8")
    assert "sky130_fd_sc_hd__sdfxtp_1" in text
    assert "scanff_faultflow" not in text


def test_scan_status_command_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`scan-status` is wired via argparse -> FlowService.scan_status ->
    Runner.scan_status. Only exercised directly against FlowService/Runner
    elsewhere; a dest= typo on the subparser itself would go undetected without
    an actual `main([...])` invocation."""
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0
    capsys.readouterr()

    assert main(["scan-status", "--top", "tiny_dff", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "top=tiny_dff" in out
    assert "scan_cells=1" in out


@pytest.mark.integration
def test_scan_techmap_command_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`scan-techmap` is wired via argparse -> FlowService.regenerate_scan_techmap
    -> Runner.scan_techmap. Requires yosys to actually regenerate the Sky130
    Verilog; skipped when unavailable, matching the existing integration-test
    pattern in this file."""
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--no-techmap"]) == 0
    capsys.readouterr()

    assert main(["scan-techmap", "--top", "tiny_dff", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "scan techmap complete top=tiny_dff" in out
    sky130_v = tmp_path / "output/tiny_dff/tiny_dff_scan.v"
    assert sky130_v.exists()
    assert "sky130_fd_sc_hd__sdfxtp_1" in sky130_v.read_text(encoding="utf-8")


def test_scan_skip_techmap_forces_run_techmap_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--skip-techmap is a legacy argparse.SUPPRESS alias (cli.py ~L218-222)
    that must force run_techmap=False regardless of --techmap/--no-techmap,
    by overriding args.techmap at dispatch (cli.py ~L378-380). Mock
    FlowService.insert_scan to capture the kwargs actually passed, rather than
    running a full techmap."""
    monkeypatch.chdir(tmp_path)
    source = _write_json(tmp_path / "tiny_dff.json", _tiny_dff_json())
    cfg = _write_config(tmp_path / "config.ofs", source)

    import faultflow.cli as cli_mod

    captured: dict[str, object] = {}

    def fake_insert_scan(_self: object, _cfg: object, **options: object) -> object:
        captured.clear()
        captured.update(options)
        return SimpleNamespace(message="scan complete (stub)")

    monkeypatch.setattr(cli_mod.FlowService, "insert_scan", fake_insert_scan)

    # --skip-techmap alone (no explicit --techmap) -> run_techmap forced False.
    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--skip-techmap"]) == 0
    assert captured["run_techmap"] is False

    # --skip-techmap even overrides an explicit --techmap request.
    assert (
        main(
            [
                "scan",
                "--top",
                "tiny_dff",
                "-c",
                str(cfg),
                "--techmap",
                "--skip-techmap",
            ]
        )
        == 0
    )
    assert captured["run_techmap"] is False

    # Sanity: without --skip-techmap, --techmap is honored as True.
    assert main(["scan", "--top", "tiny_dff", "-c", str(cfg), "--techmap"]) == 0
    assert captured["run_techmap"] is True
