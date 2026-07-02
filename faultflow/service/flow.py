from __future__ import annotations

from enum import Enum
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from faultflow.config import FaultflowConfig
from faultflow.db import connect, latest_campaign_id, summary
from faultflow.project.profiles import profile_for_cell_map
from faultflow.reporter.unified import write_unified_report
from faultflow.runner import Runner
from faultflow.scan import run_scan_techmap, write_scan_techmap
from faultflow.service.models import (
    AtpgResult,
    CampaignStatusResult,
    CleanResult,
    InitializationResult,
    NetlistWriteResult,
    OperationResult,
    ReportResult,
    RuleCheckResult,
    ScanCheckResult,
    ScanInsertionResult,
    Scalar,
    SynthesisResult,
)

log = logging.getLogger(__name__)


class ArtifactPolicy(str, Enum):
    LEGACY_CLI = "legacy_cli"
    WORKSPACE_ONLY = "workspace_only"


class FlowService:
    def __init__(
        self,
        artifact_policy: ArtifactPolicy = ArtifactPolicy.LEGACY_CLI,
        runner_factory: Callable[[FaultflowConfig], Any] = Runner,
    ) -> None:
        self.artifact_policy = artifact_policy
        self._runner_factory = runner_factory

    def _runner(self, cfg: FaultflowConfig) -> Any:
        return self._runner_factory(cfg)

    def scan_generic_json_path(self, cfg: FaultflowConfig) -> Path:
        if self.artifact_policy is ArtifactPolicy.WORKSPACE_ONLY:
            return cfg.intermediate_dir / f"{cfg.top}_scan.json"
        return cfg.scan_json_path

    def clean_campaigns(self, cfg: FaultflowConfig) -> CleanResult:
        removed = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(f"{cfg.db_path}{suffix}")
            if path.exists():
                path.unlink()
                removed += 1
        return CleanResult(
            operation="clean",
            top=cfg.top,
            message=(
                f"removed {removed} campaign database file(s)"
                if removed
                else "nothing to clean"
            ),
            removed=removed,
        )

    def has_campaign(self, cfg: FaultflowConfig, campaign_type: str) -> bool:
        """Return True only when an existing campaign has a DIFFERENT manifest hash.

        A matching manifest hash means the same scan configuration is already in the
        DB and ATPG can resume it — that is NOT stale.  An absent DB or absent
        campaign is also not stale (fresh start).
        """
        if not cfg.db_path.exists():
            return False
        with connect(cfg.db_path) as conn:
            campaign_id = latest_campaign_id(conn, campaign_type)
            if campaign_id is None:
                return False
        # There is an existing campaign — compare its manifest hash against the
        # current scan manifest so we only block on a genuinely different config.
        try:
            manifest_path = cfg.scan_manifest_path
            if not manifest_path.exists():
                return True  # No manifest yet — can't confirm a match; treat as stale
            current_hash = json.loads(manifest_path.read_text()).get(
                "generic_json_hash", ""
            )
        except Exception:
            return True  # Unreadable manifest → err on the side of caution
        with connect(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT manifest_hash FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        stored_hash = (row[0] or "") if row else ""
        return stored_hash != current_hash

    def initialize(self, cfg: FaultflowConfig) -> InitializationResult:
        message = str(self._runner(cfg).init())
        return InitializationResult("init", cfg.top, message)

    def synthesize(self, cfg: FaultflowConfig) -> SynthesisResult:
        if cfg.netlist.suffix == ".json":
            runner = self._runner(cfg)
            path = runner.find_netlist()
            log.info("synth  already synthesized: %s", path)
            return SynthesisResult(
                "synth",
                cfg.top,
                f"netlist already synthesized: {path}",
                artifacts={"netlist_json": path},
                synthesized=False,
                reason="already_synthesized",
            )
        path = self._runner(cfg).synth()
        return SynthesisResult(
            "synth",
            cfg.top,
            f"synthesis complete top={cfg.top} json={path}",
            artifacts={"netlist_json": path},
        )

    def insert_scan(
        self, cfg: FaultflowConfig, **options: object
    ) -> ScanInsertionResult:
        log.info("scan   start  top=%s", cfg.top)
        kwargs = dict(options)
        if self.artifact_policy is ArtifactPolicy.WORKSPACE_ONLY:
            kwargs["run_techmap"] = False
            kwargs["generic_json_path"] = self.scan_generic_json_path(cfg)
            kwargs["scan_report_path"] = cfg.intermediate_dir / "scan.rpt"
        message = str(self._runner(cfg).scan(**kwargs))
        return ScanInsertionResult("scan", cfg.top, message)

    def scan_status(self, cfg: FaultflowConfig) -> CampaignStatusResult:
        message = str(self._runner(cfg).scan_status())
        return CampaignStatusResult(
            "scan-status", cfg.top, message, campaign_state="available"
        )

    def check_scan(self, cfg: FaultflowConfig, **options: object) -> ScanCheckResult:
        log.info("check  start  top=%s", cfg.top)
        message = str(self._runner(cfg).scan_check(**options))
        return ScanCheckResult("scan-check", cfg.top, message)

    def rule_check(
        self, cfg: FaultflowConfig, *, strict: bool = False
    ) -> RuleCheckResult:
        log.info("rule_check  start  top=%s", cfg.top)
        from faultflow.rule_check.report import format_text

        report = self._runner(cfg).rule_check(strict=strict)
        txt = cfg.output_dir / "rule_check.rpt"
        message = format_text(report, strict=strict).rstrip("\n") + f"\nreport: {txt}"
        return RuleCheckResult(
            "rule_check",
            cfg.top,
            message,
            artifacts={"rule_check": txt},
            passed=report.passed(strict=strict),
            error_count=len(report.errors),
            warning_count=len(report.warnings),
        )

    def regenerate_scan_techmap(self, cfg: FaultflowConfig) -> OperationResult:
        log.info("techmap  start  top=%s", cfg.top)
        message = str(self._runner(cfg).scan_techmap())
        return OperationResult("scan-techmap", cfg.top, message)

    def run_atpg(self, cfg: FaultflowConfig, **options: object) -> AtpgResult:
        log.info("sim    start  top=%s", cfg.top)
        message = str(self._runner(cfg).sim(**options))
        scan = bool(options.get("scan", False))
        return AtpgResult(
            "run_atpg",
            cfg.top,
            message,
            metrics=self._campaign_metrics(cfg, "scan" if scan else "comb"),
        )

    def serial_reference(
        self, cfg: FaultflowConfig, *, scan: bool = False
    ) -> OperationResult:
        campaign_type = "scan" if scan else "comb"
        if not cfg.db_path.exists():
            raise RuntimeError("run normal ATPG/sim first")
        metrics = self._campaign_metrics(cfg, campaign_type)
        if metrics.get("campaign_state") != "available":
            raise RuntimeError("run normal ATPG/sim first")
        reports_dir = cfg.workspace_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / "serial_ref_compare.json"
        payload = {
            "top": cfg.top,
            "campaign_type": campaign_type,
            "campaign_id": metrics.get("campaign_id"),
            "status": "not_run",
            "note": (
                "Serial reference command is isolated from production coverage. "
                "Detailed serial-vs-packed mismatch enumeration is a reference "
                "diagnostic and does not update campaign state."
            ),
            "production_metrics": dict(metrics),
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return OperationResult(
            "serial_ref",
            cfg.top,
            f"serial reference report written: {path}",
            artifacts={"serial_ref": path},
            metrics=metrics,
        )

    def status(self, cfg: FaultflowConfig, scan: bool = False) -> CampaignStatusResult:
        message = str(self._runner(cfg).status(scan=scan))
        state = "not_run" if "coverage=n/a" in message else "available"
        legacy = cfg.output_dir / "faultflow.sqlite"
        warnings = (
            (f"legacy database ignored: {legacy}",)
            if legacy.exists() and not cfg.db_path.exists()
            else ()
        )
        return CampaignStatusResult(
            "status",
            cfg.top,
            message,
            metrics={
                "scan": scan,
                **self._campaign_metrics(cfg, "scan" if scan else "comb"),
            },
            warnings=warnings,
            campaign_state=state,
        )

    def _campaign_metrics(
        self, cfg: FaultflowConfig, campaign_type: str
    ) -> dict[str, Scalar]:
        if not cfg.db_path.exists():
            return {"campaign_state": "not_run"}
        with connect(cfg.db_path) as conn:
            campaign_id = latest_campaign_id(conn, campaign_type)
            if campaign_id is None:
                return {"campaign_state": "not_run"}
            data = summary(conn, campaign_id=campaign_id)
            row = conn.execute(
                """
                SELECT vector_count, atpg_terminal_reason, atpg_rounds,
                       atpg_sat, atpg_unsat, atpg_timeout, atpg_unknown,
                       atpg_generation_seconds, fault_simulation_seconds
                FROM runs WHERE campaign_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (campaign_id,),
            ).fetchone()
            run = dict(row) if row is not None else {}
        return {
            "campaign_state": "available",
            "campaign_id": campaign_id,
            "campaign_type": campaign_type,
            "terminal_reason": run.get("atpg_terminal_reason") or "n/a",
            "structural_eligible": data["structural_eligible"],
            "effective_denominator": data["denominator"],
            "detected": data["detected"],
            "redundant": data.get("redundant", 0),
            "protocol_unresolved": data.get("protocol_unresolved", 0),
            "fault_coverage_percent": data["fault_coverage_percent"],
            "test_coverage_percent": data["test_coverage_percent"],
            "vectors": run.get("vector_count", 0),
            "rounds": run.get("atpg_rounds", 0),
            "sat": run.get("atpg_sat", 0),
            "unsat": run.get("atpg_unsat", 0),
            "timeout": run.get("atpg_timeout", 0),
            "unknown": run.get("atpg_unknown", 0),
            "atpg_seconds": run.get("atpg_generation_seconds", 0.0),
            "simulation_seconds": run.get("fault_simulation_seconds", 0.0),
        }

    def run_project(
        self,
        project_path: Path,
        *,
        max_rounds: int | None = None,
        target_coverage: float | None = None,
        clean: bool = False,
    ) -> OperationResult:
        """Run per-block INTEST + assembly EXTEST, then aggregate the chip number."""
        from faultflow.project.aggregate import aggregate_project
        from faultflow.project.manifest import load_project
        from faultflow.project.orchestrator import run_project as orchestrate
        from faultflow.reporter.soc import write_soc_report

        project = load_project(project_path)
        log.info("project start  name=%s blocks=%d", project.name, len(project.blocks))
        scopes = orchestrate(
            project,
            run_atpg=self.run_atpg,
            max_rounds=max_rounds,
            target_coverage=target_coverage,
            clean=clean,
        )
        chip = aggregate_project(project.name, scopes)
        out_dir = (project.root / "output" / project.name).resolve()
        json_path, txt_path = write_soc_report(chip, out_dir)
        log.info(
            "project done  name=%s chip_coverage=%s",
            project.name,
            (
                "n/a"
                if chip.chip_coverage_percent is None
                else f"{chip.chip_coverage_percent:.3f}%"
            ),
        )
        cov = (
            "n/a"
            if chip.chip_coverage_percent is None
            else f"{chip.chip_coverage_percent:.3f}%"
        )
        return OperationResult(
            "run_project",
            project.name,
            (
                f"project complete name={project.name} scopes={len(chip.scopes)} "
                f"chip_denominator={chip.chip_denominator} "
                f"chip_detected={chip.chip_detected} chip_coverage={cov} "
                f"report={txt_path} json={json_path}"
            ),
            artifacts={"soc_report": txt_path, "soc_json": json_path},
        )

    def write_report(self, cfg: FaultflowConfig) -> ReportResult:
        log.info("report  writing  top=%s ...", cfg.top)
        path = write_unified_report(cfg)
        log.info("report  done  %s", path)
        return ReportResult(
            "report",
            cfg.top,
            f"report written: {path}",
            artifacts={"report": path},
        )

    def _write_json_verilog(
        self, cfg: FaultflowConfig, source: Path, output: Path
    ) -> Path:
        yosys = shutil.which("yosys")
        if yosys is None:
            raise RuntimeError("write_netlist requires yosys on PATH")
        cfg.ensure_workspace()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        script = cfg.generated_scripts_dir / "write_netlist.ys"
        script.write_text(
            f"read_json {{{source}}}\nwrite_verilog {{{temporary}}}\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [yosys, "-Q", "-s", str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (cfg.logs_dir / "yosys_write_netlist.log").write_text(
            proc.stdout, encoding="utf-8"
        )
        if proc.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(
                f"Yosys netlist write failed; see "
                f"{cfg.logs_dir / 'yosys_write_netlist.log'}"
            )
        os.replace(temporary, output)
        return output

    def write_netlist(
        self,
        cfg: FaultflowConfig,
        *,
        scan: bool = False,
        techmap: bool = False,
        output: Path | None = None,
    ) -> NetlistWriteResult:
        if scan:
            if not cfg.scan_manifest_path.exists():
                raise RuntimeError("scan manifest not found")
            import json

            manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
            source = Path(str(manifest["generic_json"]))
            if techmap:
                profile = profile_for_cell_map(cfg.cell_lib)
                if not profile.supports_physical_scan:
                    raise RuntimeError(
                        f"{profile.name} has no physical scan-cell binding"
                    )
                destination = output or cfg.output_dir / f"{cfg.top}_scan.v"
                techmap_v = cfg.generated_scripts_dir / "faultflow_scanff_map.v"
                write_scan_techmap(techmap_v)
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                run_scan_techmap(
                    generic_json=source,
                    techmap_verilog=techmap_v,
                    output_verilog=temporary,
                    top=cfg.top,
                    log_path=cfg.logs_dir / "yosys_scan_write.log",
                    script_path=cfg.generated_scripts_dir / "yosys_scan_write.ys",
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, destination)
            else:
                destination = output or (cfg.output_dir / f"{cfg.top}_scan_generic.v")
                self._write_json_verilog(cfg, source, destination)
        else:
            source = self._runner(cfg).find_netlist()
            destination = output or cfg.output_dir / f"{cfg.top}.v"
            self._write_json_verilog(cfg, source, destination)
        return NetlistWriteResult(
            "write_netlist",
            cfg.top,
            f"netlist written: {destination}",
            artifacts={"netlist": destination},
        )
