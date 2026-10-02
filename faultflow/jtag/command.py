"""``ff.py jtag``: grade the scan campaign's stuck-at faults with a JTAG
network-integrity program played through the TAP of the stitched netlist.

The grade runs on the scan campaign's own netlist (the scan manifest's
``generic_json``), cell map and blackboxes, so its fault rows -- ids and compiled net
indices -- are used as they are. In order: the X-isolation proof
(:mod:`faultflow.jtag.xcheck`), the golden gate
(:func:`faultflow.jtag.grade.golden_gate`), then the faults; the credit lands in side
tables (:mod:`faultflow.jtag.store`) and the coverage report is rewritten with ``jtag``
and ``combined`` blocks. A refusal writes nothing.

A design built from an autoMBIST manifest with a ``test_access`` block says how its
TAP reaches the network (EXTEST, or a dedicated IJTAG_ACCESS instruction), its
IDCODE, and the chip reset its control TDRs also clear on: the program is built for
that TAP, and that reset is pulsed during the program's TRST lead-in.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultflow.config import FaultflowConfig, JtagConfig
from faultflow.jtag.grade import FaultRow, golden_gate, grade_faults
from faultflow.jtag.program import (
    JtagProgramError,
    TckProgram,
    load_program,
    program_from_warptap,
)
from faultflow.jtag.store import latest_run, record_run, run_key
from faultflow.jtag.xcheck import ChipReset, JtagPorts, JtagSetup, analyze

if TYPE_CHECKING:
    from faultflow.integrations.autombist import AutombistTestAccess

EXTEST_OPCODE = 0  # IEEE 1149.1: all zeros


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


def _test_access(cfg: FaultflowConfig) -> AutombistTestAccess | None:
    """The [autombist] manifest's test_access block, if there is one."""
    if cfg.autombist_manifest is None:
        return None
    from faultflow.integrations.autombist import load_autombist_manifest

    return load_autombist_manifest(cfg.autombist_manifest).test_access


def network_access_opcode(access: AutombistTestAccess) -> int | None:
    """The opcode of the IJTAG_ACCESS instruction the manifest's network is behind, or
    None for EXTEST -- which a manifest without the field means."""
    name = access.network_instruction
    if name == "EXTEST":
        if access.network_opcode != EXTEST_OPCODE:
            raise JtagError(
                f"the manifest's network is behind EXTEST at opcode "
                f"{access.network_opcode:b}; EXTEST is all zeros"
            )
        return None
    if name == "IJTAG_ACCESS":
        return access.network_opcode
    raise JtagError(
        f"the manifest's network is behind {name}; ff.py jtag programs EXTEST and "
        "IJTAG_ACCESS"
    )


def chip_reset_of(access: AutombistTestAccess | None) -> ChipReset | None:
    """The chip reset the program pulses: the one the manifest's control TDRs also
    clear on, if any."""
    if access is None or access.chip_reset is None:
        return None
    return ChipReset(access.chip_reset, 0 if access.chip_reset_active_low else 1)


def tap_idcode(jtag: JtagConfig, access: AutombistTestAccess | None) -> int | None:
    """The IDCODE the program reads: the manifest's when it records the TAP's, else
    [jtag] idcode. A [jtag] idcode set against the manifest's is refused."""
    if access is None or access.idcode_value is None:
        return jtag.idcode
    if jtag.idcode_given and jtag.idcode != access.idcode_value:
        given = "none" if jtag.idcode is None else f"0x{jtag.idcode:08X}"
        raise JtagError(
            f"[jtag] idcode {given} contradicts the manifest, whose TAP has IDCODE "
            f"0x{access.idcode_value:08X}"
        )
    return access.idcode_value


def manifest_program(access: AutombistTestAccess, jtag: JtagConfig) -> TckProgram:
    """warptap's integrity program for the network the manifest describes, reached the
    way its TAP reaches it."""
    from faultflow.integrations.autombist_jtag import (
        AutombistJtagError,
        rebuild_network,
    )

    opcode = network_access_opcode(access)
    idcode = tap_idcode(jtag, access)
    try:
        graph, root = rebuild_network(access)
    except AutombistJtagError as exc:
        raise JtagError(f"{exc}; or pass --program") from exc
    return program_from_warptap(
        graph,
        root,
        ir_width=jtag.ir_width,
        has_idcode=idcode is not None,
        idcode_value=idcode if idcode is not None else 1,
        margin=jtag.margin,
        exhaustive_opcodes=jtag.exhaustive_opcodes,
        ijtag_access_opcode=opcode,
    )


def _instruction(opcode: int | None, ir_width: int) -> str:
    return "EXTEST" if opcode is None else f"IJTAG_ACCESS ({opcode:0{ir_width}b})"


def _program(
    cfg: FaultflowConfig,
    program_path: Path | None,
    access: AutombistTestAccess | None,
) -> TckProgram:
    path = program_path or cfg.jtag.program
    if path is not None:
        program = load_program(path)
        expected = None if access is None else network_access_opcode(access)
        if access is not None and program.tap.ijtag_access_opcode != expected:
            width = program.tap.ir_width
            raise JtagError(
                f"the TCK program {path} reaches the network with "
                f"{_instruction(program.tap.ijtag_access_opcode, width)}, the "
                f"manifest's TAP with {_instruction(expected, width)}"
            )
        return program
    if cfg.autombist_manifest is None:
        raise JtagError(
            "no TCK program: pass --program, set [jtag] program, or build the design "
            "from an autoMBIST manifest with test_access ([autombist] manifest)"
        )
    if access is None:
        raise JtagError(
            f"{cfg.autombist_manifest} has no test_access block: the design has no TAP "
            "to build a TCK program for"
        )
    return manifest_program(access, cfg.jtag)


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
    taps = {cfg.jtag.tck, cfg.jtag.tms, cfg.jtag.tdi, cfg.jtag.trst_n, cfg.jtag.tdo}
    for section, settings in (
        ("compression", cfg.compression),
        ("compaction", cfg.compaction),
    ):
        if settings.enabled and settings.channel_port in taps:
            raise JtagError(
                f"[{section}] channel_port is {settings.channel_port}, a TAP port's "
                f"name: name the {section} channels something else"
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
    access = _test_access(cfg)
    try:
        program = _program(cfg, program_path, access)
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
        chip_reset=chip_reset_of(access),
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
