"""``ff.py wsp``: the IEEE 1500 WSP's own faults -- those every scan test leaves to it
(``excluded_wsp``) -- graded with its integrity program (warptap's, from the wrapper's
ICL: faultflow.wrap.wsp_icl) compared at WSO (faultflow.wrap.wsp_grade).

On a block wrapped with control = wsp after its INTEST: the netlist passes the golden
gate, the grade credits most of the WSP's faults, and a WSP that doesn't behave as the
program says fails the gate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.wrap.wsp_program import serial_cells
from warptap_helpers import skip_unless_warptap, wsp_program


def _record(cells: list[tuple[str, str, int, int]]) -> dict[str, Any]:
    """A block's wrapper record with these boundary cells (side, label, chain,
    position) and a WSP."""
    roles = ("wrck", "wrstn", "selectwir", "shiftwr", "capturewr", "updatewr")
    ports = {role: {"port": role, "net": k} for k, role in enumerate(roles, 2)}
    ports |= {"wsi": {"port": "wsi", "net": 20}, "wso": {"port": "wso", "net": 21}}
    return {
        "control": "wsp",
        "wsp": {"ports": ports},
        "cells": [
            {"side": side, "label": label, "chain": chain, "position": position}
            for side, label, chain, position in cells
        ],
    }


# Serial order: chain 0's cells (a, b), then chain 1's (y).
RECORD = _record([("output", "y", 1, 0), ("input", "b", 0, 1), ("input", "a", 0, 0)])


@pytest.mark.unit
def test_the_boundary_register_runs_chain_by_chain_each_from_its_head() -> None:
    assert [c["label"] for c in serial_cells(RECORD)] == ["a", "b", "y"]


ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
WSP_PORTS = {
    role: role
    for role in ("wrck", "wrstn", "selectwir", "shiftwr", "capturewr", "updatewr")
} | {"wsi": "wsi", "wso": "wso"}
WSI_BITS = [1, 0, 1, 1, 0]


def _masked(tmp_path: Path) -> tuple[Path, list[str]]:
    """A one-flop WSI -> WSO path (t, cleared by WRSTN) with WSO = t | (m & xn),
    where xn = u0 XNOR u1, two flops that hold their power-up values (clocked from
    clk, held at 0), also the output o. xn is 1 from all-0 and from all-1 flops and
    0 from either mixed state. m is 0: every input but the WSP's is."""
    nets = {
        "wrck": 2,
        "wrstn": 3,
        "selectwir": 4,
        "shiftwr": 5,
        "capturewr": 6,
        "updatewr": 7,
        "wsi": 8,
        "m": 9,
        "clk": 15,
    }

    def cell(kind: str, **conns: int) -> dict[str, Any]:
        return {
            "type": f"sky130_fd_sc_hd__{kind}",
            "port_directions": {
                p: "output" if p in ("Q", "X", "Y") else "input" for p in conns
            },
            "connections": {p: [n] for p, n in conns.items()},
        }

    ports = {name: {"direction": "input", "bits": [net]} for name, net in nets.items()}
    ports |= {
        "wso": {"direction": "output", "bits": [20]},
        "o": {"direction": "output", "bits": [21]},
    }
    design = {
        "modules": {
            "masked": {
                "attributes": {"top": "00000000000000000000000000000001"},
                "ports": ports,
                "cells": {
                    "t": cell("dfrtp_1", CLK=2, D=8, RESET_B=3, Q=10),
                    "u0": cell("dfxtp_1", CLK=15, D=11, Q=11),
                    "u1": cell("dfxtp_1", CLK=15, D=12, Q=12),
                    "xn": cell("xnor2_1", A=11, B=12, Y=13),
                    "g": cell("a21o_1", A1=9, A2=13, B1=10, X=20),
                    "ob": cell("buf_1", A=13, X=21),
                },
                "netnames": {n: {"bits": p["bits"]} for n, p in ports.items()},
            }
        }
    }
    path = tmp_path / "masked.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path, list(nets)


def _masked_steps(o: int | None = None) -> list[Any]:
    """WRSTN low for a cycle, then WSI_BITS shifted in, WSO strobed before each
    pulse: t, the bit before. `o`: the value expected of output o there too."""
    from faultflow.wrap.wsp_model import WspCycle
    from faultflow.wrap.wsp_program import WspStep

    expected = () if o is None else (("o", o),)
    steps = [WspStep(WspCycle(wrstn=0), (), "reset", None)]
    for k, bit in enumerate(WSI_BITS):
        before = 0 if k == 0 else WSI_BITS[k - 1]
        steps.append(WspStep(WspCycle(wsi=bit), (), "shift", before, expected))
    return steps


@pytest.mark.unit
def test_the_golden_gate_refuses_an_output_only_all_0_and_all_1_flops_agree_on(
    tmp_path: Path,
) -> None:
    """o is 1 from all-0 and from all-1 flops, as expected, but 0 from a mixed
    state: the flops power up unknown, so the gate refuses."""
    from faultflow.runner.runner import _load_core
    from faultflow.wrap.wsp_grade import WspGradeError, benches, compared, golden_gate

    core = _load_core()
    if core is None:
        pytest.skip("needs the C++ core")
    netlist, inputs = _masked(tmp_path)
    steps = _masked_steps(o=1)
    expected = compared(steps, "wso")
    bench = benches(
        core,
        str(netlist),
        str(CELL_MAP),
        steps,
        inputs,
        WSP_PORTS,
        expected,
        unsupported="fail",
        blackbox=[],
    )
    with pytest.raises(WspGradeError, match="depends on the flops' initial state"):
        golden_gate(bench, steps, expected)


@pytest.mark.unit
def test_a_fault_only_all_0_and_all_1_flops_detect_is_not_credited(
    tmp_path: Path,
) -> None:
    """m stuck at 1 shows xn at WSO: from all-0 and all-1 flops WSO is then 1 where t
    is 0, but from a mixed state it is t, WSO's good value. The detection depends on
    the power-up state: init_dependent, not credited. WSI stuck at 0 is detected
    whatever the flops power up to."""
    from faultflow.runner.runner import _load_core
    from faultflow.wrap.wsp_grade import benches, compared, golden_gate, grade_faults

    core = _load_core()
    if core is None:
        pytest.skip("needs the C++ core")
    netlist, inputs = _masked(tmp_path)
    steps = _masked_steps()
    expected = compared(steps, "wso")
    bench = benches(
        core,
        str(netlist),
        str(CELL_MAP),
        steps,
        inputs,
        WSP_PORTS,
        expected,
        unsupported="fail",
        blackbox=[],
    )
    golden_gate(bench, steps, expected)
    rows = core.list_site_keys(str(netlist), str(CELL_MAP), "fail")
    index = {str(r["site_key"]): int(r["compiled_net_index"]) for r in rows}
    grade = grade_faults(
        bench, steps, [(1, index["net:9:stem"], 1), (2, index["net:8:stem"], 0)]
    )
    assert grade.init_dependent == (1,)
    assert grade.detected == {2: ("shift", 2)}  # WSI's first 1, a cycle later


@pytest.fixture
def flow_tools() -> None:
    import shutil

    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")


@pytest.mark.integration
def test_ff_py_wsp_grades_what_intest_left_to_the_wsp(
    tmp_path: Path, flow_tools: None
) -> None:
    """The ring's INTEST through the WIR, then ff.py wsp with the program warptap
    builds: every fault the INTEST left to the WSP graded, most detected, the report's
    wsp block and the side tables agreeing; graded again only with --force."""
    skip_unless_warptap("warptap.wsp_integrity")
    from faultflow.cli import main
    from wrap_flow import TOP, run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        ofs = str(tmp_path / "chip.ofs")
        assert main(["wsp", "--top", TOP, "-c", ofs]) == 0
        block = json.loads((cfg.intermediate_dir / "wsp_report.json").read_text())
        report = json.loads(
            (cfg.intermediate_dir / "coverage_report.json").read_text("utf-8")
        )
        assert main(["wsp", "--top", TOP, "-c", ofs]) == 0  # up to date
        conn = sqlite3.connect(cfg.db_path)
        runs = conn.execute("SELECT COUNT(*) FROM wsp_runs").fetchone()[0]
        detections = conn.execute("SELECT COUNT(*) FROM wsp_detections").fetchone()[0]
        conn.close()
    assert block["graded"] == report["summary"]["excluded_wsp"] > 0
    assert block["detected"] >= 0.75 * block["graded"]
    assert runs == 1 and detections == block["detected"]
    assert sum(block["detected_by_test"].values()) == block["detected"]
    assert set(block["tests"]) >= {"reset", "wir_length", "held_update"}, block
    combined = block["combined"]
    summary = report["summary"]
    # The WSP's faults join the scan's denominator, its credit the scan's.
    assert combined["detected"] == summary["detected"] + block["detected"]
    assert combined["denominator"] == summary["denominator"] + block["graded"]


@pytest.mark.integration
def test_a_wsp_that_isnt_its_model_fails_the_golden_gate(
    tmp_path: Path, flow_tools: None
) -> None:
    """WBY that never shifts (its mux's inputs swapped): the netlist's WSO isn't
    what the program expects, and nothing is graded."""
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.wrap.wsp_grade import (
        WspGradeError,
        benches,
        compared,
        golden_gate,
    )
    from wrap_flow import run_mode

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        cfg, _ = run_mode(tmp_path, "intest", wrap="control = wsp\n")
        manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
        data = json.loads(Path(manifest["generic_json"]).read_text(encoding="utf-8"))
    module = data["modules"][manifest["top"]]
    wrapper = manifest["wrapper"]
    mux = module["cells"]["__wsp_wby_d"]["connections"]
    mux["A0"], mux["A1"] = mux["A1"], mux["A0"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data), encoding="utf-8")
    scan_outs = {
        int(c["index"]): str(c["scan_out"])
        for c in manifest["chains"]
        if c["kind"] == "wrapper"
    }
    scan_ins = {
        int(c["index"]): str(c["scan_in"])
        for c in manifest["chains"]
        if c["kind"] == "wrapper"
    }
    steps = wsp_program(
        wrapper,
        scan_outs,
        scan_ins,
        top=str(manifest["top"]),
        scan_enable=str(manifest["scan_enable"]),
    ).steps
    ports = {role: str(p["port"]) for role, p in wrapper["wsp"]["ports"].items()}
    expected = compared(steps, ports["wso"])
    inputs = [
        name for name, port in module["ports"].items() if port["direction"] == "input"
    ]
    bench = benches(
        _load_core(),
        str(broken),
        str(resolve_scan_cell_map(cfg)),
        steps,
        inputs,
        ports,
        expected,
        unsupported="fail",
        blackbox=[],
    )
    with pytest.raises(WspGradeError, match="disagrees with the WSP's model"):
        golden_gate(bench, steps, expected)


@pytest.mark.integration
def test_the_shells_run_wsp_grades_after_intest(
    tmp_path: Path, flow_tools: None
) -> None:
    """The shell: the accumulator synthesized, wrapped with -control wsp, scanned,
    its INTEST, then run_wsp, the batch CLI's ff.py wsp -- with the program warptap
    builds from the wrapper's ICL."""
    skip_unless_warptap("warptap.wsp_integrity")
    from faultflow.cli import main
    from wrap_flow import ACCUMULATOR

    rtl = tmp_path / "acc.v"
    rtl.write_text(ACCUMULATOR, encoding="utf-8")
    script = tmp_path / "flow.tcl"
    script.write_text(
        "\n".join(
            [
                f"read_netlist {{{rtl}}} -top acc",
                "use_lib_cells sky130",
                "add_clock clk -off 0",
                "synth",
                "wrap -control wsp",
                "add_scan -chains 1 -SI scan_in -SO scan_out -SE scan_en",
                "check_scan",
                "set_testmode intest",
                "run_atpg -scan -max 5",
                "run_wsp",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # From the repository: the shell finds the sky130 cells there.
    assert main(["shell", "-f", str(script), "--out", str(tmp_path / "output")]) == 0
    (db,) = list((tmp_path / "output").rglob("*.sqlite"))
    conn = sqlite3.connect(db)
    (graded,) = conn.execute("SELECT graded FROM wsp_runs").fetchone()
    (detected,) = conn.execute("SELECT COUNT(*) FROM wsp_detections").fetchone()
    conn.close()
    assert graded > 0 and detected > 0
