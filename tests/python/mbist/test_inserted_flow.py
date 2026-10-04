"""The inserted chip through the whole flow: mbist-insert -c --tap-nonscan, then
init, scan, scan-check, sim --scan and ff.py jtag from the .ofs it wrote.

Scan settles each shell's reset synchronizer in a preamble, leaves the TAP and the
IJTAG network to JTAG, and credits nothing the full protocol doesn't reproduce;
ff.py jtag grades what scan left it. The scan step writes the SDC of the inserted
crossings, by pin of the scanned netlist."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from mbist_chip import LIBERTY, ROOT, chip_copy
from scan_credit import compressed_credit_not_reproduced, credit_not_reproduced
from warptap_helpers import skip_unless_warptap

CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "chip_top"
SHELLS = ("g_bank[0].u_mem", "u_core0.u_mem", "u_mem_top")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]


@pytest.fixture(scope="module")
def flowed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    from faultflow.cli import main
    from faultflow.mbist.insert import insert_command
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    skip_unless_warptap("warptap.tap_integrity")
    tmp = tmp_path_factory.mktemp("inserted_flow")
    spec = chip_copy(tmp, lambda t: t.replace("jtag: false", "jtag: {tck_max_mhz: 10}"))
    base = tmp / "chip.ofs"
    # Chains of about ten flops: every pattern replays fast.
    base.write_text(
        f"[design]\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        "[scan]\nchains = 8\n",
        encoding="utf-8",
    )
    out = tmp / "out"
    insert_command(spec, TOP, out, base, tap_nonscan=True)
    ofs = out / f"{TOP}_mbist.ofs"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(out)
        for step in (
            ["init"],
            ["scan"],
            ["scan-check"],
            ["sim", "--scan", "--export-patterns", str(out / "patterns.json")],
            ["jtag"],
        ):
            assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    return out, load_config(ofs, TOP)


def _report(cfg: Any, out: Path) -> dict[str, Any]:
    path = cfg.coverage_json_path
    path = path if path.is_absolute() else out / path
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _sdc_pins(path: Path) -> list[list[str]]:
    """The objects of each set_false_path of an SDC, unescaped."""
    return [
        [re.sub(r"\\(.)", r"\1", name) for name in objects.split()]
        for objects in re.findall(
            r"get_pins \{([^}]*)\}", path.read_text(encoding="utf-8")
        )
    ]


def test_scan_writes_the_sdc_of_the_crossings_by_pin_of_its_netlist(
    flowed: tuple[Path, Any],
) -> None:
    """Three reset synchronizers (both flops), six control synchronizers and six
    status ones (their first flop), and the TDRs' clear synchronizer: every pin
    is a pin of a cell the scanned netlist has, in the JSON and the Verilog."""
    out, cfg = flowed
    sdc = cfg.output_dir / f"{TOP}_scan.sdc"
    crossings = _sdc_pins(sdc)
    assert len(crossings) == 16
    assert sum(len(pins) for pins in crossings) == 3 * 2 + 6 + 6 + 2
    module = json.loads(cfg.scan_json_path.read_text(encoding="utf-8"))["modules"][TOP]
    verilog = cfg.scan_verilog_path.read_text(encoding="utf-8")
    for pins in crossings:
        for pin in pins:
            cell, name = pin.rsplit("/", 1)
            assert name in module["cells"][cell]["connections"], pin
            assert cell in verilog, pin


def test_scan_settles_the_synchronizers_and_leaves_the_tap_to_jtag(
    flowed: tuple[Path, Any],
) -> None:
    out, cfg = flowed
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    nonscan = {ff["instance"] for ff in manifest["ineligible_ffs"]}
    assert sum("__u_rst_sync__" in name for name in nonscan) == 2 * len(SHELLS)
    # Every TCK flop is non-scan; every chain is on clk.
    records = [r for chain in manifest["chains"] for r in chain["cell_records"]]
    assert not [r for r in records if r["instance"].startswith("warptap_")]
    assert {r["clock_net"] for r in records} == set(manifest["clock_nets"])
    patterns = json.loads((out / "patterns.json").read_text(encoding="utf-8"))
    assert patterns and {p["preamble_cycles"] for p in patterns} == {2}


def test_every_credited_fault_reproduces_in_the_full_protocol(
    flowed: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    out, cfg = flowed
    monkeypatch.chdir(out)
    assert credit_not_reproduced(cfg, out / "patterns.json") == []


def test_the_categories_add_up_and_jtag_adds_its_credit(
    flowed: tuple[Path, Any],
) -> None:
    out, cfg = flowed
    report = _report(cfg, out)
    summary, categories = report["summary"], report["autombist_categories"]
    for key in ("denominator", "detected"):
        assert sum(c[key] for c in categories.values()) == summary[key], key
    assert categories["mbist_shell"]["detected"] > 0
    jtag, combined = report["jtag"], report["combined"]
    assert jtag["detected"] > 0
    assert combined["detected"] > summary["detected"]


def test_a_control_tdr_bits_branch_into_its_synchronizer_per_fault(
    flowed: tuple[Path, Any],
) -> None:
    """The TDR bit is tied at its reset value in scan: stuck at 1, the
    synchronizer's first flop captures it (detected); stuck at 0, only freeing
    the hold would show it (hold_unresolved); the bit's own output is JTAG's."""
    out, cfg = flowed
    sdc = cfg.output_dir / f"{TOP}_scan.sdc"
    first = next(
        pins[0].rsplit("/", 1)[0]
        for pins in _sdc_pins(sdc)
        if pins[0].startswith("u_mem_top__u_sync_test_mode__")
    )
    module = json.loads(cfg.scan_json_path.read_text(encoding="utf-8"))["modules"][TOP]
    (net,) = module["cells"][first]["connections"]["D"]
    branch, stem = f"net:{net}:branch:{first}:D", f"net:{net}:stem"
    db = cfg.db_path if cfg.db_path.is_absolute() else out / cfg.db_path
    with sqlite3.connect(db) as conn:
        (campaign,) = conn.execute(
            "SELECT MAX(id) FROM campaigns WHERE campaign_type = 'scan'"
        ).fetchone()
        rows = {
            (key, kind.lower()): (status, held, exclusion)
            for key, kind, status, held, exclusion in conn.execute(
                "SELECT fault_site_key, fault_type, status, hold_unresolved, "
                "exclusion FROM faults WHERE campaign_id = ? "
                "AND fault_site_key IN (?, ?)",
                (campaign, branch, stem),
            )
        }
    assert rows[(branch, "sa1")][0] == "detected"
    assert rows[(branch, "sa0")][1] == 1
    assert rows[(stem, "sa0")][2] == rows[(stem, "sa1")][2] == "jtag"


@pytest.fixture(scope="module")
def compressed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    """The same flow with scan compression: init, scan, scan-check, scan-compress,
    sim --scan and ff.py jtag. The .ofs names the channels away from the TAP. An
    8-bit seed loads the 81 cells of 16 chains: ATPG makes only loads it can."""
    from faultflow.cli import main
    from faultflow.mbist.insert import insert_command
    from faultflow.runner.runner import _load_core

    if _load_core() is None:
        pytest.skip("needs the C++ core")
    skip_unless_warptap("warptap.tap_integrity")
    tmp = tmp_path_factory.mktemp("compressed_flow")
    spec = chip_copy(tmp, lambda t: t.replace("jtag: false", "jtag: {tck_max_mhz: 10}"))
    base = tmp / "chip.ofs"
    base.write_text(
        f"[design]\ncell_lib = {CELL_MAP}\nliberty = {LIBERTY}\n\n"
        "[scan]\nchains = 16\n\n[compression]\nenabled = true\nchannels = 8\n",
        encoding="utf-8",
    )
    out = tmp / "out"
    insert_command(spec, TOP, out, base, tap_nonscan=True)
    ofs = out / f"{TOP}_mbist.ofs"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(out)
        for step in (
            ["init"],
            ["scan"],
            ["scan-check"],
            ["scan-compress"],
            ["sim", "--scan", "--export-patterns", str(out / "patterns.json")],
            ["jtag"],
        ):
            assert main([*step, "--top", TOP, "-c", str(ofs)]) == 0, step
    return out, load_config(ofs, TOP)


def test_with_compression_the_sdc_names_the_core_and_jtag_still_grades(
    compressed: tuple[Path, Any],
) -> None:
    """The compressed netlist holds the chip as core_inst: its SDC names the same
    crossings there. The channel bus is comp_si, beside the TAP's tdi and tdo,
    and ff.py jtag grades the chip as before."""
    out, cfg = compressed
    assert cfg.compression.channel_port == "comp_si"
    composed = json.loads(
        (cfg.output_dir / f"{TOP}_compressed.json").read_text(encoding="utf-8")
    )
    module = composed["modules"][f"{TOP}_compressed"]
    assert {"comp_si", "tdi", "tdo"} <= set(module["ports"])
    crossings = _sdc_pins(cfg.output_dir / f"{TOP}_compressed.sdc")
    assert len(crossings) == 16
    for pins in crossings:
        for pin in pins:
            cell, name = pin.rsplit("/", 1)
            assert cell.startswith("core_inst__"), pin
            assert name in module["cells"][cell]["connections"], pin
    assert _report(cfg, out)["jtag"]["detected"] > 0


def test_with_compression_every_credit_holds_on_the_compressed_chip(
    compressed: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every exported pattern is one decompressor seed's whole load -- warptap's
    strictest read solves it -- and every credited fault is detected in the full
    protocol on the core and on the compressed chip itself, that seed on comp_si
    and the synthesized decompressor loading the chains. What only a load the
    decompressor can't make would test is compression_unresolved."""
    from warptap.faultflow_compression import (
        care_bit_rows,
        polynomial_from_manifest,
        solve_pattern_seed,
    )

    out, cfg = compressed
    monkeypatch.chdir(out)
    patterns = out / "patterns.json"
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    compression = manifest["compression"]
    poly = polynomial_from_manifest(compression)
    rows = care_bit_rows(
        poly, compression["phase_shifter_taps"], int(manifest["max_chain_length"])
    )

    def seed_of(number: int, raw: dict[str, Any]) -> int:
        # Every load position a hard constraint (no load_care).
        return int(solve_pattern_seed(raw["load_seqs"], rows, poly.width, number))

    exported = json.loads(patterns.read_text(encoding="utf-8"))
    assert exported
    for number, raw in enumerate(exported):
        seed_of(number, raw)
    assert credit_not_reproduced(cfg, patterns) == []
    assert compressed_credit_not_reproduced(cfg, patterns, seed_of) == {
        "not_reproduced": [],
        "golden": [],
    }
    assert _report(cfg, out)["summary"]["compression_unresolved"] > 0
