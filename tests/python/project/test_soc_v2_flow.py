"""The faultflow_project_v2 flow on the SoC soc_v2_fixtures builds: two blocks Yosys
synthesizes (an accumulator and a counter), each wrapped with the IEEE 1500 wrapper
ff.py wrap puts on and scanned on its own; glue that strings their wrapper chains
into one, brings their core chains and mode pins to SoC pins, and routes the
interconnect through inverters."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.project.manifest import load_soc_project
from faultflow.project.soc_flow import build_soc
from soc_v2_fixtures import write_soc_v2
from wrap_flow import manifest_validator


@pytest.fixture
def flow_tools() -> None:
    from faultflow.runner.runner import _load_core

    if shutil.which("yosys") is None or _load_core() is None:
        pytest.skip("needs Yosys on PATH and the C++ core")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("needs iverilog")


@pytest.mark.integration
def test_the_soc_is_composed_traced_and_recorded(
    tmp_path: Path, flow_tools: None
) -> None:
    """The SoC's chains traced on its cells, one wrapper record over both blocks'
    rings, its sky130 Verilog, and the inputs soc.hold names held in its tests --
    unless the base config holds one of them the other way."""
    import dataclasses

    from faultflow.project.manifest import ProjectError

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        project = load_soc_project(write_soc_v2(tmp_path, hold={"en": 1}))
        base = load_config(project.base_config, project.soc.top)
        against = dataclasses.replace(
            base, scan=dataclasses.replace(base.scan, hold=(("en", 0),))
        )
        with pytest.raises(ProjectError, match="soc.hold holds en at 1"):
            build_soc(project, against, tmp_path / "out")
        build = build_soc(project, base, tmp_path / "out")
        manifest = json.loads(build.cfg.scan_manifest_path.read_text("utf-8"))
        verilog = Path(manifest["sky130_verilog"]).read_text(encoding="utf-8")
    assert build.cfg.scan.hold == (("en", 1),)
    manifest_validator().validate(manifest)
    chains = [(c["scan_in"], c["scan_out"], c["kind"]) for c in manifest["chains"]]
    assert chains == [
        ("si_a", "so_a", "core"),
        ("si_b", "so_b", "core"),
        ("wsi", "wso", "wrapper"),
    ]
    assert manifest["scan_enable"] == "scan_en"
    wrapper = manifest["wrapper"]
    assert wrapper["holds"]["extest"] == {"t_intest": 0, "t_extest": 1}
    assert wrapper["holds"]["intest"] == {"t_intest": 1, "t_extest": 0}
    labels = [cell["label"] for cell in wrapper["cells"]]
    # A's ring, then B's, along the one wrapper chain.
    assert labels[0].startswith("blkA/") and labels[-1].startswith("blkB/")
    assert [cell["position"] for cell in wrapper["cells"]] == list(range(len(labels)))
    assert "$scanff" not in verilog and "sky130_fd_sc_hd__sdfxtp_1" in verilog


@pytest.mark.integration
def test_the_project_runs_and_counts_every_fault_once(
    tmp_path: Path, flow_tools: None
) -> None:
    """Each block's INTEST and the SoC's EXTEST, then the chip number: every fault
    counted once, each one a scope leaves to another graded there. The SoC's EXTEST
    patterns replay on the composed SoC's cells, and their every credit is
    reproduced there."""
    from faultflow.project.soc_flow import run_soc_project, soc_chip_coverage
    from faultflow.service.flow import FlowService
    from scan_credit import credit_not_reproduced
    from scan_replay import replay_on_cells

    patterns = tmp_path / "soc_extest.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        project = load_soc_project(write_soc_v2(tmp_path))
        base = load_config(project.base_config, project.soc.top)
        service = FlowService()
        scopes = run_soc_project(
            project,
            base,
            run_atpg=service.run_atpg,
            check_scan=service.check_scan,
            out=tmp_path / "out",
            soc_patterns=patterns,
        )
        chip = soc_chip_coverage(project, scopes)
        # The SoC's EXTEST patterns on its cells.
        replayed = replay_on_cells(scopes.soc, patterns, tmp_path / "replay")
        reproduced = credit_not_reproduced(
            scopes.soc, patterns, campaign_type="scan_extest"
        )
        report = json.loads(scopes.soc.coverage_json_path.read_text("utf-8"))
    assert chip.guards["handoffs_owned"]
    assert [s.name for s in chip.scopes] == ["blkA", "blkB", "soc"]
    assert all(s.owned > 0 for s in chip.scopes)
    assert chip.chip_denominator == sum(s.owned for s in chip.scopes)
    percent = chip.chip_coverage_percent
    assert percent is not None and percent > 80.0
    assert replayed == []
    assert reproduced == []
    # The SoC's EXTEST grades its glue and the boundary cells; the cores are
    # stubs, whatever of them it reads left to INTEST.
    parts = report["wrapper"]["parts"]
    assert set(parts) == {"core", "boundary", "mode", "glue"}
    assert parts["glue"]["denominator"] > 0 and parts["glue"]["detected"] > 0
    assert parts["boundary"]["denominator"] > 0
    assert parts["core"]["denominator"] == 0


@pytest.mark.integration
def test_hierarchical_coverage_holds_against_the_flat_soc(
    tmp_path: Path, flow_tools: None
) -> None:
    """The flat comparison: one functional scan test of the whole composed SoC, its
    wrappers in functional mode. Every fault it detects is detected by the
    hierarchical tests under one of its identities, or explained: a wrapper fault
    no mode can test (blackbox or hold unresolved, redundant), one a block's
    boundary or the scan path accounts for. And the chip number is the flat one's,
    within a point."""
    import sqlite3

    from faultflow.project.identity import BlockSites, soc_identities
    from faultflow.project.soc_flow import run_soc_project, soc_chip_coverage
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map
    from faultflow.service.flow import FlowService

    core = _load_core()
    assert core is not None
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        project = load_soc_project(write_soc_v2(tmp_path))
        base = load_config(project.base_config, project.soc.top)
        service = FlowService()
        scopes = run_soc_project(
            project,
            base,
            run_atpg=service.run_atpg,
            check_scan=service.check_scan,
            out=tmp_path / "out",
        )
        chip = soc_chip_coverage(project, scopes)
        flat = scopes.build.cfg
        assert flat.test_mode == "functional"
        service.run_atpg(flat, scan=True)
        cell_map = str(resolve_scan_cell_map(flat))
        composed = flat.scan_json_path
        soc_rows = list(core.list_site_keys(str(composed), cell_map, "fail", []))
        sites = {}
        for instance, cfg in scopes.blocks.items():
            module = json.loads(cfg.scan_json_path.read_text("utf-8"))["modules"][
                cfg.top
            ]
            inputs = frozenset(
                bit
                for port in module["ports"].values()
                if port["direction"] == "input"
                for bit in port["bits"]
                if isinstance(bit, int)
            )
            rows = tuple(
                dict(r)
                for r in core.list_site_keys(
                    str(cfg.scan_json_path), cell_map, "fail", []
                )
            )
            name = scopes.build.blocks[instance][0]
            sites[instance] = BlockSites(
                name, rows, inputs, scopes.build.remaps[instance]
            )
        identities = soc_identities(soc_rows, sites)

        def statuses(path: Path, campaign_type: str) -> dict[tuple[str, str], Any]:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT fault_site_key AS k, lower(fault_type) AS t, status, "
                "exclusion, blackbox_unresolved AS bb, hold_unresolved AS hold "
                "FROM faults WHERE campaign_id = (SELECT MAX(id) FROM campaigns "
                "WHERE campaign_type = ?)",
                (campaign_type,),
            ).fetchall()
            conn.close()
            return {(row["k"], row["t"]): row for row in rows}

        flat_faults = statuses(flat.db_path, "scan")
        flat_report = json.loads(flat.coverage_json_path.read_text("utf-8"))
        hier: dict[tuple[tuple[str, str], str], Any] = {}
        for instance, cfg in scopes.blocks.items():
            name = scopes.build.blocks[instance][0]
            for (key, fault_type), row in statuses(cfg.db_path, "scan").items():
                hier[((name, key), fault_type)] = row
        soc_ids = soc_identities(
            core.list_site_keys(
                str(scopes.soc.intermediate_dir / "soc_top_extest.json"),
                cell_map,
                "fail",
                ["u_a__core", "u_b__core"],
            ),
            sites,
            stubs=["u_a__core", "u_b__core"],
        )
        extest = statuses(scopes.soc.db_path, "scan_extest")
        for (key, fault_type), row in extest.items():
            for identity in soc_ids.get(key, [("soc", key)]):
                if row["exclusion"] == "none":
                    hier[(identity, fault_type)] = row
                else:
                    hier.setdefault((identity, fault_type), row)
    unexplained = []
    for (key, fault_type), row in flat_faults.items():
        if row["status"] != "detected":
            continue
        found = [
            hier[(identity, fault_type)]
            for identity in identities[key]
            if (identity, fault_type) in hier
        ]
        if any(f["status"] == "detected" for f in found):
            continue
        if any(f["bb"] or f["hold"] or f["exclusion"] != "none" for f in found) or any(
            f["status"] == "redundant" for f in found
        ):
            continue
        unexplained.append((key, fault_type, identities[key]))
    assert unexplained == []
    flat_percent = flat_report["summary"]["test_coverage_percent"]
    assert chip.chip_coverage_percent is not None
    assert chip.chip_coverage_percent >= flat_percent - 1.0
