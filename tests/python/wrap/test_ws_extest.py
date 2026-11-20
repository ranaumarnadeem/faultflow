"""EXTEST through the wrapper serial port: ``[wrap] extest_instruction = WS_EXTEST``
with a wrapper an IEEE 1500 WSP controls (``control = wsp``). The WIR loads
WS_EXTEST, and the boundary register is one chain from WSI to WSO -- the wrapper
chains in serial order, shifting with ShiftWR on WRCK -- whose cells the mode
observes capture while CaptureWR is 1 (faultflow.wrap.serial)."""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import ConfigError, FaultflowConfig, load_config
from faultflow.wrap.errors import WrapError
from wrap_flow import CELL_MAP_PATH, scanned

TOP = "chip"
WSP = "control = wsp\n"
SERIAL = WSP + "extest_instruction = WS_EXTEST\n"
TWO_CHAINS = "chains = 1\nwrapper_chains = 2\n"


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _chip(
    tmp_path: Path, wrap: str = WSP
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """The ring wrapped with a WSP, its 3 boundary cells on 2 wrapper chains,
    scanned in `tmp_path`, the working directory: its config, scan manifest and
    cell map."""
    ofs = scanned(tmp_path, wrap=wrap, scan=TWO_CHAINS)
    cfg = load_config(ofs, TOP)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    return cfg, manifest, json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_the_extest_instruction_in_the_config(tmp_path: Path) -> None:
    ofs = tmp_path / "w.ofs"
    ofs.write_text("[wrap]\nenabled = true\ncontrol = wsp\n", encoding="utf-8")
    assert load_config(ofs, TOP).wrap.extest_instruction == "WP_EXTEST"
    ofs.write_text(
        "[wrap]\ncontrol = wsp\nextest_instruction = ws_extest\n", encoding="utf-8"
    )
    assert load_config(ofs, TOP).wrap.extest_instruction == "WS_EXTEST"
    # A SoC's config sets it without wrapping anything: the scan manifest says
    # whether a WSP controls the wrapper (test_ws_extest_needs_a_wsp).
    ofs.write_text("[wrap]\nextest_instruction = WS_EXTEST\n", encoding="utf-8")
    assert load_config(ofs, TOP).wrap.extest_instruction == "WS_EXTEST"
    ofs.write_text(
        "[wrap]\ncontrol = wsp\nextest_instruction = WS_INTEST\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="WP_EXTEST or WS_EXTEST"):
        load_config(ofs, TOP)


@pytest.mark.unit
def test_a_serial_extest_is_a_campaign_of_its_own(tmp_path: Path) -> None:
    """The serial EXTEST grades through another access than the parallel one: it
    can't resume one. Keyed only for it, so every campaign of today keeps its
    fingerprint."""
    from faultflow.runner.runner import Runner

    ofs = tmp_path / "w.ofs"
    ofs.write_text(
        "[design]\nnetlist = n.json\ncell_lib = c.json\n"
        "[wrap]\ncontrol = wsp\nextest_instruction = WS_EXTEST\n",
        encoding="utf-8",
    )
    serial = load_config(ofs, TOP)
    parallel = dataclasses.replace(
        serial, wrap=dataclasses.replace(serial.wrap, extest_instruction="WP_EXTEST")
    )

    def payload(cfg: FaultflowConfig, mode: str) -> dict[str, object]:
        return Runner(
            dataclasses.replace(cfg, test_mode=mode)
        )._config_fingerprint_payload()

    assert payload(serial, "extest")["wrap_extest_instruction"] == "WS_EXTEST"
    assert "wrap_extest_instruction" not in payload(parallel, "extest")
    assert "wrap_extest_instruction" not in payload(serial, "intest")
    assert "wrap_extest_instruction" not in payload(serial, "functional")


@pytest.mark.integration
def test_the_serial_chain_runs_from_wsi_through_every_wrapper_chain_to_wso(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traced on the cells, each WIR at WS_EXTEST: the wrapper chains in their
    order, joined head to tail -- the same flops the manifest's wrapper chains
    hold -- shifting on ShiftWR, clocked by WRCK."""
    from faultflow.wrap.serial import serial_manifest

    monkeypatch.chdir(tmp_path)
    _, manifest, cell_map = _chip(tmp_path)
    wrapper = [c for c in manifest["chains"] if c["kind"] == "wrapper"]
    assert len(wrapper) == 2
    serial = serial_manifest(manifest, cell_map)
    (chain,) = serial["chains"]
    assert chain["cells"] == [name for c in wrapper for name in c["cells"]]
    assert (chain["scan_in"], chain["scan_out"]) == ("wsi", "wso")
    assert (serial["scan_inputs"], serial["scan_outputs"]) == (["wsi"], ["wso"])
    assert serial["scan_enable"] == "shiftwr"
    assert serial["clock_ports"] == ["wrck"]
    assert [r["chain_position"] for r in serial["cells"]] == list(range(3))
    assert serial["serial"] == {
        "instruction": "WS_EXTEST",
        "holds": {
            "wrstn": 1,
            "selectwir": 0,
            "updatewr": 0,
            "capturewr": 1,
            manifest["scan_enable"]: 0,
            **{c["scan_in"]: 0 for c in wrapper},
        },
        "shift_values": {"capturewr": 0},
        "unobserved": [c["scan_out"] for c in wrapper],
        "parallel_scan_enable": manifest["scan_enable"],
        "parallel_scan_inputs": [c["scan_in"] for c in wrapper],
    }


@pytest.mark.integration
def test_a_serial_path_the_cells_dont_have_is_refused(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second wrapper chain's head mux cut off the first chain's tail: no
    path from WSI reaches it."""
    from faultflow.wrap.serial import serial_manifest

    monkeypatch.chdir(tmp_path)
    _, manifest, cell_map = _chip(tmp_path)
    data = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    module = copy.deepcopy(data["modules"][TOP])
    head = module["cells"]["__wsp_head_2"]
    head["connections"]["A1"] = ["0"]
    with pytest.raises(WrapError, match="neither wsi nor a scan cell's Q"):
        serial_manifest(manifest, cell_map, module)


@pytest.mark.integration
def test_ws_extest_needs_a_wsp(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faultflow.wrap.serial import serial_manifest

    monkeypatch.chdir(tmp_path)
    _, manifest, cell_map = _chip(tmp_path, wrap="")
    with pytest.raises(WrapError, match="IEEE 1500 WSP"):
        serial_manifest(manifest, cell_map)


def _statuses(cfg: Any) -> dict[tuple[str, str], tuple[str, str]]:
    """The latest EXTEST campaign's faults: (site key, type) -> (status,
    exclusion)."""
    from wrap_flow import faults

    return {
        (str(r["fault_site_key"]), str(r["type"])): (
            str(r["status"]),
            str(r["exclusion"]),
        )
        for r in faults(cfg, "scan_extest")
    }


@pytest.mark.integration
def test_ws_extest_patterns_shift_the_boundary_register_through_the_wsp(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pattern starts by loading WS_EXTEST into the WIR, then shifts the
    boundary register from WSI to WSO on ShiftWR, CaptureWR at 1 for the capture
    alone. The patterns replay on the whole block's cells (the core never clocked,
    its flops unknown) and as STIL, both overlap modes; every credit is reproduced
    in the full protocol. And they credit exactly what WP_EXTEST's credit, fault
    for fault, with the same denominator: only the access to the boundary register
    differs."""
    from faultflow.scan.setup import setup_to_list
    from faultflow.wrap.wsp_setup import instruction_program
    from scan_credit import core_dependent, credit_not_reproduced
    from scan_replay import replay_on_cells
    from wrap_flow import run_mode

    serial_dir, parallel_dir = tmp_path / "ws", tmp_path / "wp"
    for work in (serial_dir, parallel_dir):
        work.mkdir()
    monkeypatch.chdir(serial_dir)
    cfg, patterns = run_mode(serial_dir, "extest", wrap=SERIAL, scan=TWO_CHAINS)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    program = setup_to_list(instruction_program(manifest["wrapper"], "WS_EXTEST"))
    assert exported
    for pattern in exported:
        assert pattern["access"] == "WS_EXTEST"
        assert pattern["setup"] == program
        assert list(pattern["load_seqs"]) == ["0"]
        assert pattern["shift_pi_values"] == {"capturewr": False}
        assert pattern["capture_pi_values"]["capturewr"] is True
        assert "shift_length" not in pattern
    assert replay_on_cells(cfg, patterns, serial_dir / "replay") == []
    # write-patterns writes the STIL of the chip's serial chain, WSI to WSO.
    from faultflow.cli import main

    stil = serial_dir / "ws_extest.stil"
    run = ["write-patterns", "--top", TOP, "-c", str(serial_dir / "chip.ofs")]
    assert main([*run, "--patterns", str(patterns), "-o", str(stil)]) == 0
    text = stil.read_text(encoding="utf-8")
    assert text.count("ScanChain ") == 1
    assert 'ScanIn "wsi";' in text and 'ScanOut "wso";' in text
    assert "ScanLength 3;" in text
    assert credit_not_reproduced(cfg, patterns, campaign_type="scan_extest") == []
    serial = _statuses(cfg)

    monkeypatch.chdir(parallel_dir)
    parallel_cfg, parallel_patterns = run_mode(
        parallel_dir, "extest", wrap=WSP, scan=TWO_CHAINS
    )
    # The serial patterns leave the core unclocked; the parallel ones load it, and
    # every one of their credits holds whatever it holds -- the same credits (below).
    assert core_dependent(parallel_cfg, parallel_patterns) == []
    parallel = _statuses(parallel_cfg)
    assert set(serial) == set(parallel)
    detected = {key for key, (status, _) in serial.items() if status == "detected"}
    assert detected
    # Every fault has the same status. What each access shifts through is its own
    # scan path (ShiftWR and WSI in WS_EXTEST; the scan enable and the wrapper
    # chains' own scan inputs in WP_EXTEST): excluded either way, under another
    # category.
    assert {k: s for k, (s, _) in serial.items()} == {
        k: s for k, (s, _) in parallel.items()
    }
    moved = {k for k in serial if serial[k][1] != parallel[k][1]}
    assert moved and all(serial[k][0] == "excluded" for k in moved)


@pytest.mark.integration
@pytest.mark.parametrize("model", ["stuck-at", "los"])
def test_a_synthesized_blocks_ws_extest_patterns_replay_on_its_cells(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """The accumulator Yosys synthesizes, its reset unwrapped, on 2 wrapper chains:
    WS_EXTEST's patterns replay on its cells, and launching on shift, the last shift
    of the boundary register from WSI launches. Stuck-at, every credit is reproduced
    and is WP_EXTEST's."""
    from scan_credit import credit_not_reproduced
    from scan_replay import replay_on_cells
    from wrap_flow import ACCUMULATOR, run_mode

    sections = "[clocks]\ngroups = clk wrck\n"
    if model == "los":
        sections = (
            "[fault_model]\nmodel = transition\nlaunch = los\ncollapsing = false\n\n"
            + sections
        )
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    runs = {}
    for name, wrap in (("ws", SERIAL), ("wp", WSP)):
        work = tmp_path / name
        work.mkdir()
        monkeypatch.chdir(work)
        runs[name] = run_mode(
            work, "extest", sections, netlist=rtl, top="acc", wrap=wrap, scan=TWO_CHAINS
        )
        if name == "wp" and model == "los":
            break
    cfg, patterns = runs["ws"]
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    assert exported and all(p["access"] == "WS_EXTEST" for p in exported)
    if model == "los":
        assert all(p["launch"] == "los" for p in exported)
    monkeypatch.chdir(tmp_path / "ws")
    assert replay_on_cells(cfg, patterns, tmp_path / "ws" / "replay") == []
    if model == "los":
        return
    assert credit_not_reproduced(cfg, patterns, campaign_type="scan_extest") == []
    serial, parallel = _statuses(cfg), _statuses(runs["wp"][0])
    assert {k: s for k, (s, _) in serial.items()} == {
        k: s for k, (s, _) in parallel.items()
    }


@pytest.mark.integration
def test_a_socs_ws_extest_shifts_every_blocks_boundary_register_wsi_to_wso(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SoC of two WSP blocks, WSI -> u_a -> u_b -> WSO (soc_v2_fixtures'
    GLUE_WSP_RTL), EXTEST in WS_EXTEST: one chain through both blocks' boundary
    registers, u_a's first; the program loads WS_EXTEST into both WIRs. The chip
    number counts every fault once; the SoC's patterns replay on its cells and
    every credit is reproduced; and they credit what WP_EXTEST's do."""
    from faultflow.project.manifest import load_soc_project
    from faultflow.project.soc_flow import run_soc_project, soc_chip_coverage
    from faultflow.scan.setup import setup_to_list
    from faultflow.service.flow import FlowService
    from faultflow.wrap.serial import serial_manifest
    from faultflow.wrap.wsp_setup import instruction_program
    from scan_credit import credit_not_reproduced
    from scan_replay import replay_on_cells
    from soc_v2_fixtures import write_soc_v2
    from warptap_helpers import _importable

    runs: dict[str, Any] = {}
    for name, base in (
        ("ws", "[wrap]\nextest_instruction = WS_EXTEST\n"),
        ("wp", ""),
    ):
        work = tmp_path / name
        work.mkdir()
        monkeypatch.chdir(work)
        project = load_soc_project(write_soc_v2(work, base=base, wsp=True))
        service = FlowService()
        patterns = work / "soc_extest.json"
        scopes = run_soc_project(
            project,
            load_config(project.base_config, project.soc.top),
            run_atpg=service.run_atpg,
            check_scan=service.check_scan,
            out=work / "out",
            soc_patterns=patterns,
        )
        runs[name] = (scopes, patterns, soc_chip_coverage(project, scopes))
    scopes, patterns, chip = runs["ws"]
    monkeypatch.chdir(tmp_path / "ws")
    manifest = json.loads(scopes.build.cfg.scan_manifest_path.read_text("utf-8"))
    cell_map = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
    serial = serial_manifest(manifest, cell_map)
    (chain,) = serial["chains"]
    blocks = [name.split("__")[0] for name in chain["cells"]]
    assert blocks == sorted(blocks) and set(blocks) == {"u_a", "u_b"}
    wrapper_flops = {
        n for c in manifest["chains"] if c["kind"] == "wrapper" for n in c["cells"]
    }
    assert set(chain["cells"]) == wrapper_flops
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    program = setup_to_list(instruction_program(manifest["wrapper"], "WS_EXTEST"))
    assert len(program) == 1 + 2 * 3 + 1  # WRSTN, both instructions, the update
    assert exported and all(
        p["access"] == "WS_EXTEST" and p["setup"] == program for p in exported
    )
    if _importable("warptap.pdl_1500"):
        # The SoC's chain of WIRs and boundary registers as warptap retargets
        # 'iWrite u0.WIR WS_EXTEST; iWrite u1.WIR ...' and each pattern's
        # 'iWrite u<i>.WBR<k> ...' (test_ws_extest_is_what_warptaps_pdl_...).
        from faultflow.wrap.wsp_pdl import check_serial_loads

        pdl = check_serial_loads(
            manifest["wrapper"],
            serial,
            exported,
            {},
            top="soc_top",
            scan_ins={},
            scan_enable=str(manifest["scan_enable"]),
        )
        assert "iWrite u1.WBR0" in pdl
    assert chip.guards["handoffs_owned"]
    assert chip.chip_denominator == sum(s.owned for s in chip.scopes)
    assert replay_on_cells(scopes.soc, patterns, tmp_path / "ws" / "replay") == []
    assert (
        credit_not_reproduced(scopes.soc, patterns, campaign_type="scan_extest") == []
    )
    serial_rows, parallel_rows = _statuses(scopes.soc), _statuses(runs["wp"][0].soc)
    assert {k: s for k, (s, _) in serial_rows.items()} == {
        k: s for k, (s, _) in parallel_rows.items()
    }
    assert chip.chip_coverage_percent == runs["wp"][2].chip_coverage_percent


@pytest.mark.integration
def test_ws_extest_is_what_warptaps_pdl_retargets_to(
    tmp_path: Path, flow_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup program is warptap's retarget of 'iWrite WIR WS_EXTEST'; the
    boundary register traced on the cells, WSI to WSO, is the ICL's two WBR
    registers in order; and each pattern's load is the WSI bits warptap shifts for
    'iWrite WBR0 ...; iWrite WBR1 ...; iApply'. A chain traced in another order
    is caught."""
    from faultflow.wrap.serial import serial_manifest
    from faultflow.wrap.wsp_pdl import check_serial_loads
    from faultflow.wrap.wsp_program import WspProgramError
    from warptap_helpers import skip_unless_warptap
    from wrap_flow import run_mode

    skip_unless_warptap("warptap.pdl_1500")
    monkeypatch.chdir(tmp_path)
    cfg, patterns = run_mode(tmp_path, "extest", wrap=SERIAL, scan=TWO_CHAINS)
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    cell_map = json.loads(CELL_MAP_PATH.read_text(encoding="utf-8"))
    serial = serial_manifest(manifest, cell_map)
    exported = json.loads(patterns.read_text(encoding="utf-8"))
    wrapper_chains = [c for c in manifest["chains"] if c["kind"] == "wrapper"]
    scan_outs = {int(c["index"]): str(c["scan_out"]) for c in wrapper_chains}
    scan_ins = {int(c["index"]): str(c["scan_in"]) for c in wrapper_chains}
    scan_enable = str(manifest["scan_enable"])

    def check(serial: dict[str, Any]) -> str:
        return check_serial_loads(
            manifest["wrapper"],
            serial,
            exported,
            scan_outs,
            top=TOP,
            scan_ins=scan_ins,
            scan_enable=scan_enable,
        )

    pdl = check(serial)
    assert "iWrite WIR WS_EXTEST" in pdl
    assert pdl.count("iWrite WBR0") == pdl.count("iWrite WBR1") == len(exported)
    shuffled = copy.deepcopy(serial)
    cells = shuffled["chains"][0]["cells"]
    cells[0], cells[-1] = cells[-1], cells[0]
    with pytest.raises(WspProgramError, match="isn't the ICL's"):
        check(shuffled)
