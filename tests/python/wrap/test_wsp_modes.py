"""Scan tests of a wrapper its IEEE 1500 WSP controls ([wrap] control = wsp): every
pattern starts with the program that loads the mode's instruction into the WIR --
WP_INTEST, WP_EXTEST, or WS_BYPASS for the functional test -- and the test holds
the WSP still (faultflow.wrap.wsp_setup).

The programs put the instruction in force in the WSP's behavioural model, a SoC's
chain of WIRs and inverted pins included. Each flow's exported patterns, the
program first, replay on the block's sky130 cells -- with the wrapped inputs at X
for INTEST, on the whole block for EXTEST -- both overlap modes, and as STIL
(scan_replay); without the program they don't. Every fault they credit is detected
in the full scan protocol, program included, whatever the environment or the core
holds (scan_credit). The WSP's own faults are left to its own test
(``excluded_wsp``); where a control net enters a boundary cell, the fault is the
cell's, owned by the mode that activates it.

The ring: clk 2, d0 4, d1 5; r0 = d0 ^ r2, r1 = r0 & d1, r2 = r1 | d0;
y = r2 ^ r0."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.setup import SetupCycle, setup_to_list
from faultflow.wrap.errors import WrapError
from faultflow.wrap.wsp_model import OPCODES, WspCycle, WspModel
from faultflow.wrap.wsp_setup import (
    HELD,
    MODE_INSTRUCTIONS,
    WspAccess,
    holds,
    mode_program,
    program,
)
from scan_credit import core_dependent, credit_not_reproduced, environment_dependent
from scan_replay import replay_on_cells
from wrap_flow import ACCUMULATOR, run_mode
from wrap_flow import faults as fault_rows

WSP = "control = wsp\n"
ENVIRONMENT = ["d0", "d1"]
GROUPED = "[clocks]\ngroups = clk wrck\n"


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


def _at_wsp(access: WspAccess, cycle: SetupCycle, wsi: int | None = None) -> WspCycle:
    """A program's cycle as the WSPs see it, through the pins' inverters."""
    level = {
        role: cycle.value(name) ^ int(inverted)
        for role, (name, inverted) in access.pins.items()
    }
    return WspCycle(
        wrstn=level["wrstn"],
        selectwir=level["selectwir"],
        shiftwr=level["shiftwr"],
        capturewr=level["capturewr"],
        updatewr=level["updatewr"],
        wsi=level["wsi"] if wsi is None else wsi,
        pulse=access.wrck in cycle.pulse,
    )


def _run_chain(
    access: WspAccess, cycles: tuple[SetupCycle, ...], length: int
) -> list[WspModel]:
    """The program on a chain of `length` WSPs, WSI to WSO: each one's WSI the
    WSO of the one before at the cycle's strobe."""
    models = [WspModel([]) for _ in range(length)]
    for cycle in cycles:
        first = _at_wsp(access, cycle)
        wsi = first.wsi
        steps = []
        for model in models:
            step = _at_wsp(access, cycle, wsi)
            wsi = model.wso(step) or 0
            steps.append(step)
        for model, step in zip(models, steps):
            model.step(step)
    return models


@pytest.mark.unit
@pytest.mark.parametrize("mode", sorted(MODE_INSTRUCTIONS))
def test_a_mode_program_puts_its_instruction_in_force_and_the_holds_keep_it(
    mode: str,
) -> None:
    record = {
        "control": "wsp",
        "wsp": {
            "ports": {
                role: {"port": role, "net": k}
                for k, role in enumerate(("wrck", *HELD, "wso"), start=2)
            }
        },
    }
    access = WspAccess({role: (role, False) for role in HELD}, "wrck")
    cycles = mode_program(record, mode)
    # WRSTN, the three bits, the update.
    assert len(cycles) == 5 and not cycles[0].pulse
    assert [c.pulse for c in cycles[1:]] == [("wrck",)] * 4
    (model,) = _run_chain(access, cycles, 1)
    assert model.instruction == MODE_INSTRUCTIONS[mode]
    assert model.wby == 0
    held = WspCycle(pulse=True)  # its defaults are the holds
    assert {role: getattr(held, role) for role in HELD} == HELD
    for _ in range(3):
        model.step(held)
    assert model.instruction == MODE_INSTRUCTIONS[mode] and model.wby == 0
    assert holds(access) == HELD


@pytest.mark.unit
def test_a_chain_of_wirs_takes_each_its_own_instruction_through_inverted_pins() -> None:
    """The last WIR's code goes in first; an inverter on a pin's way flips the chip
    input the program and the holds drive."""
    access = WspAccess(
        {
            "wrstn": ("rst", True),
            "selectwir": ("sel", False),
            "shiftwr": ("sh", False),
            "capturewr": ("cap", False),
            "updatewr": ("upd", True),
            "wsi": ("si", False),
        },
        "tck",
        wirs=3,
    )
    codes = [OPCODES["WP_INTEST"], OPCODES["WP_EXTEST"], OPCODES["WS_INTEST"]]
    cycles = program(access, codes)
    assert len(cycles) == 1 + 3 * 3 + 1
    # Every cycle names every WSP input: rst and upd sit behind inverters.
    names = {"rst", "sel", "sh", "cap", "upd", "si"}
    assert all({name for name, _ in cycle.inputs} == names for cycle in cycles)
    models = _run_chain(access, cycles, 3)
    assert [m.instruction for m in models] == [
        "WP_INTEST",
        "WP_EXTEST",
        "WS_INTEST",
    ]
    assert holds(access) == {"rst": 0, "sel": 0, "sh": 0, "cap": 0, "upd": 1, "si": 0}
    with pytest.raises(WrapError, match="3 WIR"):
        program(access, codes[:2])
    shared = WspAccess(
        {**access.pins, "shiftwr": ("sel", False), "updatewr": ("sel", False)},
        "tck",
    )
    with pytest.raises(WrapError, match="sel"):
        program(shared, [OPCODES["WP_INTEST"]])


def _wsp_cells(cfg: Any) -> set[str]:
    from faultflow.wrap.wsp import WSP_ATTR

    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    module = json.loads(Path(manifest["generic_json"]).read_text("utf-8"))["modules"]
    return {
        name
        for name, cell in module[str(manifest["top"])]["cells"].items()
        if WSP_ATTR in cell.get("attributes", {})
    }


def _consumer(site_key: str) -> str | None:
    parts = site_key.split(":")
    return parts[3] if parts[2] == "branch" else None


def _net(site_key: str) -> int:
    return int(site_key.split(":")[1])


def _check_wsp_faults(
    cfg: Any, campaign_type: str = "scan"
) -> dict[tuple[str, str], Any]:
    """No fault on a branch into a WSP cell is graded: the WSP's test takes it --
    unless it is the scan enable's, a scan input's or WRCK's -- and no fault where
    a control net enters a boundary cell is the WSP's. The faults, by key."""
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    wrapper = manifest["wrapper"]
    wsp = _wsp_cells(cfg)
    rows = {(r["fault_site_key"], r["type"]): r for r in fault_rows(cfg, campaign_type)}
    into_wsp = [row for key, row in rows.items() if _consumer(key[0]) in wsp]
    graded = [
        row
        for row in into_wsp
        if row["exclusion"] == "none" and row["collapsed_into"] is None
    ]
    assert graded == []
    assert {row["exclusion"] for row in into_wsp} <= {
        "wsp",
        "scan_internal",
        "scan_chain",
        "clock",
        "reset",  # WRSTN clears the WIR and WBY
        "none",  # collapsed
    }
    assert sum(row["exclusion"] == "wsp" for row in into_wsp) > 30
    boundary = {
        str(cell[role]) for cell in wrapper["cells"] for role in ("mux", "gate")
    }
    nets = wrapper["wsp"]["nets"]
    controls = {int(nets[n]) for n in ("in_hold", "in_safe", "out_hold", "out_safe")}
    branched = {_net(key[0]) for key in rows if _consumer(key[0]) is not None}
    # A control net's branch into a cell, or its stem where one cell alone reads it.
    entering = [
        row
        for key, row in rows.items()
        if _net(key[0]) in controls
        and (
            _consumer(key[0]) in boundary
            or (_consumer(key[0]) is None and _net(key[0]) not in branched)
        )
    ]
    assert entering and all(row["exclusion"] != "wsp" for row in entering)
    return rows


@pytest.mark.integration
def test_wsp_intest_patterns_load_wp_intest_and_test_the_core_whatever_drives_it(
    tmp_path: Path, flow_tools: None
) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(tmp_path, "intest", wrap=WSP)
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        intest = setup_to_list(mode_program(manifest["wrapper"], "intest"))
        assert exported
        for pattern in exported:
            assert pattern["setup"] == intest
            values = pattern["capture_pi_values"]
            assert {role: int(values[role]) for role in HELD} == HELD
            assert not {"d0", "d1", "y"} & set(values)
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=ENVIRONMENT
        )
        assert problems == []
        assert credit_not_reproduced(cfg, patterns) == []
        assert environment_dependent(cfg, patterns, ENVIRONMENT) == []
        # Without the program the WIR isn't WP_INTEST: the cells don't hold.
        bare = tmp_path / "no_setup.json"
        bare.write_text(
            json.dumps([{**p, "setup": []} for p in exported]), encoding="utf-8"
        )
        assert replay_on_cells(
            cfg, bare, tmp_path / "replay_bare", unknown_inputs=ENVIRONMENT
        )
        rows = _check_wsp_faults(cfg)
        report = json.loads(
            (cfg.intermediate_dir / "coverage_report.json").read_text("utf-8")
        )
        text = cfg.coverage_report_path.read_text(encoding="utf-8")
    summary = report["summary"]
    assert summary["excluded_wsp"] == sum(
        row["exclusion"] == "wsp" for row in rows.values()
    )
    assert summary["excluded_wsp"] > 0 and summary["excluded_jtag"] == 0
    assert f"excluded_wsp:        {summary['excluded_wsp']}" in text
    # The system side is EXTEST's.
    for cell in manifest["wrapper"]["cells"]:
        for fault_type in ("sa0", "sa1"):
            row = rows[(f"net:{cell['sys_net']}:stem", fault_type)]
            assert row["exclusion"] == "wbr_decoupled"
    parts = report["wrapper"]["parts"]
    for key, total in (
        ("detected", "detected"),
        ("denominator", "denominator"),
        ("decoupled", "excluded_wbr_decoupled"),
        ("blackbox_unresolved", "blackbox_unresolved"),
    ):
        assert sum(part[key] for part in parts.values()) == summary[total], key
    # The mode's part: where the decode's nets enter the cells.
    assert parts["mode"]["detected"] > 0 and parts["mode"]["decoupled"] > 0
    assert parts["mode"]["blackbox_unresolved"] >= 1
    assert parts["core"]["detected"] > 0


@pytest.mark.integration
@pytest.mark.parametrize("launch", ["loc", "los"])
def test_wsp_intest_transition_patterns_with_wrck_grouped_with_the_core_clock(
    tmp_path: Path, flow_tools: None, launch: str
) -> None:
    """The boundary cells run on WRCK, the core on clk: grouped ([clocks] groups),
    a path between them is one domain's, launched and captured in one test."""
    sections = (
        f"[fault_model]\nmodel = transition\nlaunch = {launch}\n"
        f"collapsing = false\n\n{GROUPED}"
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(tmp_path, "intest", sections, wrap=WSP)
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        assert exported and {p["launch"] for p in exported} == {launch}
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=ENVIRONMENT
        )
        assert problems == []
        if launch == "loc":
            assert credit_not_reproduced(cfg, patterns, loc=True) == []
            assert environment_dependent(cfg, patterns, ENVIRONMENT, loc=True) == []
        rows = _check_wsp_faults(cfg)
    assert not [r for r in rows.values() if r["exclusion"] == "cross_domain"]
    assert any(r["status"] == "detected" for r in rows.values())


@pytest.mark.integration
def test_wsp_extest_patterns_load_wp_extest_and_test_the_boundary(
    tmp_path: Path, flow_tools: None
) -> None:
    """The graybox keeps the WSP, which the program sets to WP_EXTEST; the patterns
    replay on the whole block's cells, the core's chains along."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(tmp_path, "extest", wrap=WSP)
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        exported = json.loads(patterns.read_text(encoding="utf-8"))
        extest = setup_to_list(mode_program(manifest["wrapper"], "extest"))
        assert exported and all(p["setup"] == extest for p in exported)
        assert replay_on_cells(cfg, patterns, tmp_path / "replay") == []
        assert credit_not_reproduced(cfg, patterns, campaign_type="scan_extest") == []
        assert core_dependent(cfg, patterns) == []
        rows = _check_wsp_faults(cfg, "scan_extest")
    for cell in manifest["wrapper"]["cells"]:
        for fault_type in ("sa0", "sa1"):
            row = rows[(f"net:{cell['sys_net']}:stem", fault_type)]
            assert row["status"] == "detected", (cell["label"], fault_type)
    assert not [key for key in rows if ":g_x:" in key[0] or ":r0:" in key[0]]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "model"),
    [
        ("intest", "stuck-at"),
        ("intest", "loc"),
        ("intest", "los"),
        ("extest", "stuck-at"),
        ("extest", "los"),
    ],
)
def test_a_synthesized_blocks_wsp_patterns_hold_whatever_surrounds_it(
    tmp_path: Path, flow_tools: None, mode: str, model: str
) -> None:
    """The accumulator Yosys synthesizes, its reset unwrapped and b[1] left
    unwrapped."""
    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    sections = GROUPED
    if model != "stuck-at":
        sections = (
            f"[fault_model]\nmodel = transition\nlaunch = {model}\n"
            f"collapsing = false\n\n{GROUPED}"
        )
    environment = ["en", "a[0]", "a[1]", "b[0]"]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, patterns = run_mode(
            tmp_path,
            mode,
            sections,
            netlist=rtl,
            top="acc",
            wrap=WSP + "exclude = b[1]\n",
        )
        assert json.loads(patterns.read_text(encoding="utf-8"))
        unknown = environment if mode == "intest" else []
        problems = replay_on_cells(
            cfg, patterns, tmp_path / "replay", unknown_inputs=unknown
        )
        assert problems == []
        if model == "los":
            return
        loc = model == "loc"
        if mode == "intest":
            assert credit_not_reproduced(cfg, patterns, loc=loc) == []
            assert environment_dependent(cfg, patterns, environment, loc=loc) == []
        else:
            check = credit_not_reproduced(cfg, patterns, campaign_type="scan_extest")
            assert check == []
            assert core_dependent(cfg, patterns) == []


@pytest.mark.integration
def test_a_wsp_wrappers_scan_test_refuses_a_test_setup_of_its_own(
    tmp_path: Path, flow_tools: None, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = tmp_path / "setup.json"
    setup.write_text(
        json.dumps(
            {"format": "faultflow_test_setup_v1", "cycles": [{"inputs": {"d0": 1}}]}
        ),
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        with pytest.raises((AssertionError, SystemExit)):
            run_mode(tmp_path, "intest", f"test_setup = {setup}\n", wrap=WSP)
    captured = capsys.readouterr()
    assert "instruction into the WSP is the test setup" in captured.out + captured.err
