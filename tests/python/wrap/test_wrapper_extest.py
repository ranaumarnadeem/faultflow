"""EXTEST of the IEEE 1500 wrapper ff.py wrap puts on: a scan test of the block's
graybox -- its boundary cells, the core one unknown blackbox (faultflow.wrap.graybox)
-- with the mode pins held at (0, 1): each output cell drives its port from its flop,
each input cell captures its port, and the core is held off.

Each flow's exported patterns load the wrapper chains alone (shift_length) and replay
on the whole block's sky130 cells, both overlap modes, and as STIL (scan_replay);
every fault they credit is detected on the whole block in the full scan protocol, by
one pattern whatever state the core is in (scan_credit.core_dependent)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scan_credit import core_dependent, credit_not_reproduced
from scan_replay import replay_on_cells
from wrap_flow import ACCUMULATOR, TOP, run_mode
from wrap_flow import faults as fault_rows


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


@pytest.mark.integration
def test_extest_patterns_test_the_boundary_whatever_the_core_holds(
    tmp_path: Path, flow_tools: None
) -> None:
    """INTEST first, then EXTEST: a campaign of its own, in the same workspace."""
    from faultflow.cli import main

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest")
        patterns = tmp_path / "extest_patterns.json"
        extest = ["extest", "--export-patterns", str(patterns), "--top", TOP]
        assert main([*extest, "-c", str(tmp_path / "chip.ofs")]) == 0
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        wrapper_chains = {
            int(c["index"]): int(c["length"])
            for c in manifest["chains"]
            if c["kind"] == "wrapper"
        }
        assert exported
        for pattern in exported:
            values = pattern["capture_pi_values"]
            assert (values["wbr_intest"], values["wbr_extest"]) == (False, True)
            assert set(pattern["load_seqs"]) == {str(c) for c in wrapper_chains}
            assert pattern["shift_length"] == max(wrapper_chains.values())
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
        assert credit_not_reproduced(cfg, patterns, campaign_type="scan_extest") == []
        assert core_dependent(cfg, patterns) == []
        report = json.loads(
            (cfg.intermediate_dir / "coverage_report.json").read_text("utf-8")
        )
        extest_faults = {
            (row["fault_site_key"], row["type"]): row
            for row in fault_rows(cfg, "scan_extest")
        }
        assert fault_rows(cfg, "scan")  # INTEST's campaign is still there
    wrapper = manifest["wrapper"]
    extest_net = wrapper["extest"]["net"]
    # EXTEST stuck at 0 would let the core out through the output cells.
    assert extest_faults[(f"net:{extest_net}:stem", "sa0")]["blackbox_unresolved"] == 1
    for cell in wrapper["cells"]:
        for fault_type in ("sa0", "sa1"):
            row = extest_faults[(f"net:{cell['sys_net']}:stem", fault_type)]
            assert row["status"] == "detected", (cell["label"], fault_type)
    # No fault of the core is in EXTEST's campaign.
    assert not [key for key in extest_faults if ":g_x:" in key[0] or ":r0:" in key[0]]
    assert report["wrapper"]["mode"] == "extest"
    parts = report["wrapper"]["parts"]
    assert parts["boundary"]["detected"] > 0 and parts["boundary"]["decoupled"] > 0


@pytest.mark.integration
def test_extest_launches_on_shift_and_refuses_launch_on_capture(
    tmp_path: Path, flow_tools: None
) -> None:
    """Launch on capture launches nothing at the boundary -- the output cells hold
    and the input cells capture the same port values twice -- so EXTEST at speed
    launches on shift."""
    from faultflow.cli import main

    transition = "[fault_model]\nmodel = transition\ncollapsing = false\n"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(tmp_path, "extest", transition + "launch = los\n")
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported and {p["launch"] for p in exported} == {"los"}
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
        ofs = tmp_path / "chip.ofs"
        ofs.write_text(
            ofs.read_text(encoding="utf-8").replace("launch = los", "launch = loc"),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            main(["extest", "--clean", "--top", TOP, "-c", str(ofs)])


@pytest.mark.integration
@pytest.mark.parametrize("model", ["stuck-at", "los"])
def test_a_synthesized_blocks_extest_patterns_hold_whatever_its_core_holds(
    tmp_path: Path, flow_tools: None, model: str
) -> None:
    """The accumulator Yosys synthesizes, b[1] left unwrapped: in EXTEST it reaches
    only the core, which no test observes."""
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    fault_model = (
        ""
        if model == "stuck-at"
        else "[fault_model]\nmodel = transition\nlaunch = los\ncollapsing = false\n"
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(
            tmp_path,
            "extest",
            fault_model,
            netlist=rtl,
            top="acc",
            wrap="exclude = b[1]\n",
        )
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
        if model == "stuck-at":
            check = credit_not_reproduced(cfg, patterns, campaign_type="scan_extest")
            assert check == []
            assert core_dependent(cfg, patterns) == []
