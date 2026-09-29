"""``ff.py jtag``: grade the scan campaign's stuck-at faults with a JTAG
network-integrity program played through the TAP of the stitched netlist.

The grade runs on the scan campaign's own netlist (the scan manifest's
``generic_json``), cell map and blackboxes, so its fault rows -- ids and compiled net
indices -- are used as they are. In order: the X-isolation proof
(:mod:`faultflow.jtag.xcheck`), the golden gate
(:func:`faultflow.jtag.grade.golden_gate`), then the faults; the credit lands in side
tables (:mod:`faultflow.jtag.store`) and the coverage report is rewritten with ``jtag``
and ``combined`` blocks. A refusal writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.config import FaultflowConfig
from faultflow.jtag.grade import FaultRow, golden_gate, grade_faults
from faultflow.jtag.program import (
    JtagProgramError,
    TckProgram,
    load_program,
    program_from_warptap,
)
from faultflow.jtag.store import latest_run, record_run, run_key
from faultflow.jtag.xcheck import JtagPorts, JtagSetup, analyze


class JtagError(RuntimeError):
    """``ff.py jtag`` can't run as asked; the message says why."""


@dataclass(frozen=True)
class JtagOutcome:
    graded: int
    detected: int
    reset_path: int
    periods: int
    seconds: float
    up_to_date: bool
    verified: bool
    combined_coverage: float | None = None  # scan + jtag test coverage, percent

    def message(self, top: str) -> str:
        combined = (
            "n/a"
            if self.combined_coverage is None
            else f"{self.combined_coverage:.3f}%"
        )
        return (
            f"jtag top={top} periods={self.periods} graded={self.graded} "
            f"detected={self.detected} reset_path_ungraded={self.reset_path} "
            f"combined_coverage={combined}"
            + (" verified=true" if self.verified else "")
            + (
                " up_to_date=true"
                if self.up_to_date
                else f" seconds={self.seconds:.2f}"
            )
        )


def _combined_coverage(conn: Any, campaign_id: int) -> float | None:
    from faultflow.db import summary
    from faultflow.jtag.store import report_blocks

    blocks = report_blocks(conn, campaign_id, summary(conn, campaign_id=campaign_id))
    return None if blocks is None else blocks[1]["test_coverage_percent"]


def _require_scan_campaign(conn: Any) -> int:
    from faultflow.db.campaign import latest_campaign_id

    campaign_id = latest_campaign_id(conn, "scan")
    if campaign_id is None:
        raise JtagError("no scan campaign: run 'ff.py sim --scan' first")
    run = conn.execute(
        "SELECT completed_at FROM runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    if run is None or run["completed_at"] is None:
        raise JtagError(
            "the scan campaign has no completed run: finish 'ff.py sim --scan'"
        )
    return int(campaign_id)


def _program(cfg: FaultflowConfig, program_path: Path | None) -> TckProgram:
    path = program_path or cfg.jtag.program
    if path is not None:
        return load_program(path)
    if cfg.autombist_manifest is None:
        raise JtagError(
            "no TCK program: pass --program, set [jtag] program, or build the design "
            "from an autoMBIST manifest with test_access ([autombist] manifest)"
        )
    from faultflow.integrations.autombist import load_autombist_manifest
    from faultflow.integrations.autombist_jtag import (
        AutombistJtagError,
        rebuild_network,
    )

    access = load_autombist_manifest(cfg.autombist_manifest).test_access
    if access is None:
        raise JtagError(
            f"{cfg.autombist_manifest} has no test_access block: the design has no TAP "
            "to build a TCK program for"
        )
    try:
        graph, root = rebuild_network(access)
    except AutombistJtagError as exc:
        raise JtagError(f"{exc}; or pass --program") from exc
    return program_from_warptap(
        graph,
        root,
        ir_width=cfg.jtag.ir_width,
        has_idcode=cfg.jtag.idcode is not None,
        idcode_value=cfg.jtag.idcode if cfg.jtag.idcode is not None else 1,
        margin=cfg.jtag.margin,
        exhaustive_opcodes=cfg.jtag.exhaustive_opcodes,
    )


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_jtag(
    cfg: FaultflowConfig,
    *,
    program_path: Path | None = None,
    force: bool = False,
    sim_threads: int | None = None,
    export: Path | None = None,
    verify: bool = False,
    reference: bool = False,
) -> JtagOutcome:
    """Grade, record and report; see the module docstring."""
    from faultflow.config import resolve_sim_threads
    from faultflow.db import connect
    from faultflow.reporter.coverage import write_reports
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map

    if cfg.fault_model.model != "stuck_at":
        raise JtagError(
            "ff.py jtag grades stuck-at faults only; fault_model is transition"
        )
    if cfg.compression.enabled or cfg.compaction.enabled:
        raise JtagError(
            "ff.py jtag can't grade a design with [compression] or [compaction]: "
            "their channel ports are named tdi/tdo, the TAP's own port names"
        )
    core = _load_core()
    if core is None:
        raise JtagError("C++ extension _faultflow_core is required")
    if not cfg.scan_manifest_path.exists():
        raise JtagError(
            "no scan manifest: run 'ff.py scan' and 'ff.py sim --scan' first"
        )
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    netlist = Path(str(manifest["generic_json"]))
    cell_map = resolve_scan_cell_map(cfg)
    unsupported = cfg.simulation.unsupported_cells
    blackbox = list(cfg.blackbox_instances)
    try:
        program = _program(cfg, program_path)
    except JtagProgramError as exc:
        raise JtagError(str(exc)) from exc
    if export is not None:
        export.write_text(
            json.dumps(program.to_json(), indent=1) + "\n", encoding="utf-8"
        )

    start = time.perf_counter()
    jcfg = cfg.jtag
    setup: JtagSetup = analyze(
        core,
        netlist,
        cell_map,
        str(manifest["top"]),
        ports=JtagPorts(
            tck=jcfg.tck, tms=jcfg.tms, tdi=jcfg.tdi, trst_n=jcfg.trst_n, tdo=jcfg.tdo
        ),
        holds=dict(jcfg.hold),
        unsupported=unsupported,
        blackbox_instances=blackbox,
    )
    golden = golden_gate(
        core,
        netlist,
        cell_map,
        program,
        setup,
        unsupported=unsupported,
        blackbox=blackbox,
    )
    verified = False
    if verify:
        from faultflow.jtag.verify import verify_on_gate_level

        verify_on_gate_level(cfg, manifest, program, setup, golden)
        verified = True

    key = run_key(program, _file_digest(netlist), setup, blackbox)
    with connect(cfg.db_path) as conn:
        campaign_id = _require_scan_campaign(conn)
        previous = latest_run(conn, campaign_id)
        if previous is not None and previous["run_key"] == key and not force:
            return JtagOutcome(
                graded=int(previous["graded"]),
                detected=int(
                    conn.execute(
                        "SELECT COUNT(*) FROM jtag_detections WHERE run_id = ?",
                        (int(previous["id"]),),
                    ).fetchone()[0]
                ),
                reset_path=int(previous["reset_path"]),
                periods=len(program),
                seconds=time.perf_counter() - start,
                up_to_date=True,
                verified=verified,
                combined_coverage=_combined_coverage(conn, campaign_id),
            )
        faults = [
            FaultRow(
                id=int(row["id"]),
                site_key=str(row["fault_site_key"]),
                fault_type=str(row["fault_type"]).lower(),
                compiled_net_index=int(row["compiled_net_index"]),
            )
            for row in conn.execute(
                """
                SELECT id, fault_site_key, fault_type, compiled_net_index
                FROM faults
                WHERE campaign_id = ? AND exclusion IN ('none', 'jtag')
                  AND collapsed_into IS NULL
                ORDER BY id
                """,
                (campaign_id,),
            )
        ]
    threads = resolve_sim_threads(
        sim_threads if sim_threads is not None else cfg.simulation.sim_threads
    )
    grade = grade_faults(
        core,
        netlist,
        cell_map,
        program,
        setup,
        faults,
        unsupported=unsupported,
        blackbox=blackbox,
        sim_threads=threads,
        reference=reference,
    )
    with connect(cfg.db_path) as conn:
        record_run(conn, campaign_id, key, program, setup, grade)
        write_reports(
            conn,
            cfg,
            scan_context={
                "pseudo_port_map": {},
                "manifest_hash": str(manifest.get("generic_json_hash", "")),
            },
            campaign_id=campaign_id,
        )
        combined = _combined_coverage(conn, campaign_id)
    return JtagOutcome(
        graded=grade.graded,
        detected=len(grade.detected),
        reset_path=len(grade.reset_path),
        periods=len(program),
        seconds=time.perf_counter() - start,
        up_to_date=False,
        verified=verified,
        combined_coverage=combined,
    )
