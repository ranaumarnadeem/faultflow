from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from faultflow.config import (
    AtpgConfig,
    ClockSpec,
    ConfigError,
    FaultflowConfig,
    FaultModelConfig,
    ReportConfig,
    ScanConfig,
    SimulationConfig,
    parse_bool_value,
    parse_timeout_schedule,
)
from faultflow.project.profiles import TechnologyProfile, get_profile
from faultflow.service import ArtifactPolicy, FlowService, OperationResult
from faultflow.shell.errors import ShellError, precondition, unsupported


@dataclass(frozen=True)
class SessionSnapshot:
    top: str | None
    source: str | None
    source_kind: str | None
    profile: str | None
    synthesized: bool
    scan_inserted: bool
    scan_checked: bool
    scan_campaign_stale: bool
    options: tuple[tuple[str, str], ...]


class ProjectSession:
    def __init__(
        self,
        *,
        output_root: Path = Path("output"),
        service: Any | None = None,
        baseline_config: FaultflowConfig | None = None,
    ) -> None:
        self.output_root = output_root
        self.service = service or FlowService(
            artifact_policy=ArtifactPolicy.WORKSPACE_ONLY
        )
        self.baseline_config = baseline_config
        self.top: str | None = None
        self.source: Path | None = None
        self.source_kind: str | None = None
        self.profile: TechnologyProfile | None = None
        self.synthesized = False
        self.scan_inserted = False
        self.scan_checked = False
        self.scan_campaign_stale = False
        self.options: dict[str, str] = {}
        self.declared_clocks: list[ClockSpec] = []
        self.declared_blackbox: list[str] = []
        self.test_mode: str = "functional"

    @property
    def checkpoint_path(self) -> Path:
        if self.top is None:
            raise precondition("no design loaded", "NO_DESIGN")
        return self.output_root / self.top / ".faultflow" / "session.json"

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            top=self.top,
            source=str(self.source) if self.source is not None else None,
            source_kind=self.source_kind,
            profile=self.profile.name if self.profile is not None else None,
            synthesized=self.synthesized,
            scan_inserted=self.scan_inserted,
            scan_checked=self.scan_checked,
            scan_campaign_stale=self.scan_campaign_stale,
            options=tuple(sorted(self.options.items())),
        )

    def _checkpoint_payload(self) -> dict[str, object]:
        snap = asdict(self.snapshot())
        snap["output_root"] = str(self.output_root)
        snap["declared_clocks"] = [
            [cs.port, cs.off_state] for cs in self.declared_clocks
        ]
        snap["declared_blackbox"] = list(self.declared_blackbox)
        snap["test_mode"] = self.test_mode
        return snap

    def checkpoint(self) -> tuple[Path, str]:
        path = self.checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._checkpoint_payload(), indent=2, sort_keys=True) + "\n"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        return path, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def read_netlist(self, path: Path, top: str) -> OperationResult:
        if self.top is not None:
            raise precondition(
                f"design '{self.top}' already loaded; use reset first",
                "NO_DESIGN",
            )
        if not path.exists():
            raise ShellError(f"netlist not found: {path}", "INPUT", "FILE_NOT_FOUND")
        suffix = path.suffix.lower()
        if suffix == ".sv":
            raise unsupported("SystemVerilog is not supported", "SYSTEMVERILOG")
        if suffix not in {".v", ".json"}:
            raise ShellError(
                f"unsupported netlist extension: {suffix}",
                "INPUT",
                "UNSUPPORTED_EXTENSION",
            )
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            modules = data.get("modules", {})
            if not isinstance(modules, dict) or top not in modules:
                raise ShellError(
                    f"top module '{top}' not found in {path}",
                    "INPUT",
                    "TOP_NOT_FOUND",
                )
        self.top = top
        self.source = path
        self.source_kind = "yosys_json" if suffix == ".json" else "verilog"
        self.synthesized = suffix == ".json"
        self.checkpoint()
        return OperationResult("read_netlist", top, f"loaded {path}")

    def use_lib_cells(self, name: str) -> OperationResult:
        profile = get_profile(name)
        self.profile = profile
        if self.top is not None:
            self.scan_checked = False
            self.checkpoint()
        return OperationResult(
            "use_lib_cells",
            self.top or "",
            f"using technology profile {profile.name}",
        )

    def add_clock(self, port: str, *, off_state: int = 0) -> None:
        if off_state not in (0, 1):
            raise ShellError(
                f"add_clock: off_state must be 0 or 1, got {off_state}",
                "CONFIG",
                "INVALID_VALUE",
            )
        self.declared_clocks = [cs for cs in self.declared_clocks if cs.port != port]
        self.declared_clocks.append(ClockSpec(port=port, off_state=off_state))
        if self.top is not None:
            self.checkpoint()

    def report_clocks(self) -> list[dict[str, object]]:
        return [
            {"port": cs.port, "off_state": cs.off_state} for cs in self.declared_clocks
        ]

    def add_blackbox(self, instance: str) -> None:
        instance = instance.strip()
        if not instance:
            raise ShellError(
                "add_blackbox: instance name must not be empty",
                "CONFIG",
                "INVALID_VALUE",
            )
        if instance not in self.declared_blackbox:
            self.declared_blackbox.append(instance)
        if self.top is not None:
            self.checkpoint()

    def report_blackbox(self) -> list[str]:
        return list(self.declared_blackbox)

    def set_testmode(self, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in {"functional", "intest", "extest"}:
            raise ShellError(
                "set_testmode: mode must be functional, intest, or extest, "
                f"got {mode!r}",
                "CONFIG",
                "INVALID_VALUE",
            )
        self.test_mode = normalized
        if self.top is not None:
            self.checkpoint()

    def report_testmode(self) -> str:
        return self.test_mode

    def check_cells(self, *, allow: list[str] | None = None) -> dict[str, object]:
        """Audit the configured netlist against the selected PDK cell map.

        Report-only (like report_clocks): never aborts the session. Returns a
        dict with the total cell count, uncovered (would-be-blackboxed) cell
        types, memory/macro-like types, and an ``ok`` flag.
        """
        if self.top is None or self.source is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        if self.source_kind != "yosys_json":
            raise precondition(
                "check_cells needs a synthesized Yosys JSON netlist; run synth first",
                "SYNTH_REQUIRED",
            )
        if self.profile is None:
            raise precondition("run use_lib_cells first", "PROFILE_REQUIRED")

        from faultflow.reporter.cell_audit import audit_netlist

        result = audit_netlist(
            self.source,
            self.profile.cell_map,
            list(allow or []),
            top=self.top,
        )
        return {
            "netlist": str(result.netlist),
            "cellmap": str(result.cellmap),
            "top": result.top,
            "total_cells": result.total_cells,
            "unique_types": result.unique_types,
            "uncovered": [{"type": t, "count": n} for t, n in result.uncovered],
            "allowed_uncovered": [
                {"type": t, "count": n} for t, n in result.allowed_uncovered
            ],
            "memory_like": [{"type": t, "count": n} for t, n in result.mem_like],
            "ok": result.ok,
        }

    # ---------------------------------------------------------------------- #
    # Test-point insertion (Path A — add_tp / reject_tp)
    # ---------------------------------------------------------------------- #

    def _tp_stack_path(self) -> Path:
        if self.top is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        return self.output_root / self.top / "tp" / "state.json"

    def add_tp(
        self,
        *,
        metric: str | None = None,
        threshold: int | None = None,
        max_points: int | None = None,
    ) -> dict[str, object]:
        """Insert test points via OT, run new campaign, return comparison dict."""
        if self.top is None or self.source is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        if not self.synthesized:
            raise precondition("run synth first", "SYNTH_REQUIRED")

        cfg = self.materialize_config()
        tp_cfg = cfg.testpoint

        # Ensure baseline campaign exists.
        from faultflow.db import connect, init_schema, latest_campaign_id

        if not cfg.db_path.exists():
            raise precondition(
                "run sim first to establish a baseline campaign",
                "NO_BASELINE_CAMPAIGN",
            )
        with connect(cfg.db_path) as conn:
            baseline_campaign = latest_campaign_id(conn, "comb")
        if baseline_campaign is None:
            raise precondition(
                "no combinational campaign found; run sim first",
                "NO_BASELINE_CAMPAIGN",
            )

        from faultflow.testpoint.versions import TestpointVersionStack

        stack = TestpointVersionStack.load_or_create(self._tp_stack_path())
        if stack.current is None:
            stack.push(self.source, baseline_campaign, label="baseline")

        # Determine output path for TPI netlist.
        tp_dir = self._tp_stack_path().parent / f"iter{len(stack.all())}"
        tp_dir.mkdir(parents=True, exist_ok=True)
        tp_netlist = tp_dir / f"{self.top}_tp.json"

        from faultflow.testpoint.opentest_driver import insert_test_points

        _lib = str(cfg.cell_lib.name).lower()
        _tech = "osu035" if ("osu035" in _lib or "osu" in _lib) else "sky130"

        result = insert_test_points(
            tp_cfg.opentest,
            self.source,
            tp_netlist,
            metric=metric or tp_cfg.metric,
            threshold=threshold if threshold is not None else tp_cfg.threshold,
            max_points=max_points if max_points is not None else tp_cfg.max_points,
            tech=_tech,
        )

        # Run ATPG on TPI netlist.
        import dataclasses

        tp_sim_cfg = dataclasses.replace(cfg, netlist=tp_netlist)
        self.service.initialize(tp_sim_cfg)
        self.service.run_atpg(tp_sim_cfg)

        with connect(tp_sim_cfg.db_path) as conn:
            tp_campaign = latest_campaign_id(conn, "comb")
        if tp_campaign is None:
            raise RuntimeError("TPI ATPG produced no campaign")

        stack.push(
            tp_netlist,
            tp_campaign,
            label=f"tp_iter{len(stack.all()) - 1}",
        )

        # Build comparison.
        from faultflow.testpoint.compare import (
            build_comparison,
            compute_rescued,
            load_summary,
        )

        with connect(tp_sim_cfg.db_path) as conn:
            init_schema(conn)
            baseline_sum = load_summary(conn, baseline_campaign)
            tp_sum = load_summary(conn, tp_campaign)
            rescued = compute_rescued(conn, baseline_campaign, tp_campaign)

        comparison = build_comparison(
            baseline_sum,
            tp_sum,
            rescued,
            tp_report={
                "total": result.total_count,
                "obs": result.obs_count,
                "ctrl": result.ctrl_count,
            },
        )

        self.checkpoint()
        return comparison

    def reject_tp(self) -> str:
        """Revert to the previous TP iteration (or baseline)."""
        from faultflow.testpoint.versions import TestpointVersionStack

        stack = TestpointVersionStack.load_or_create(self._tp_stack_path())
        if stack.current is None or len(stack.all()) <= 1:
            raise ShellError(
                "no test-point iteration to reject (already at baseline)",
                "TP",
                "NO_TP_TO_REJECT",
            )
        popped = stack.pop()
        prev = stack.current
        if prev is not None:
            self.source = Path(prev.netlist_path)
        self.checkpoint()
        return (
            f"rejected {popped.label}; "
            f"restored to {prev.label if prev else 'baseline'}"
        )

    def materialize_config(self) -> FaultflowConfig:
        if self.top is None or self.source is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        if self.profile is None:
            raise precondition("run use_lib_cells first", "PROFILE_REQUIRED")
        if self.baseline_config is not None:
            cfg = replace(
                self.baseline_config,
                top=self.top,
                netlist=self.source,
                cell_lib=self.profile.cell_map,
                liberty=self.profile.liberty,
                verilog_models=self.profile.verilog_models,
                output_root=self.output_root,
                clocks=tuple(self.declared_clocks),
                blackbox_instances=tuple(self.declared_blackbox),
                test_mode=self.test_mode,
            )
        else:
            cfg = FaultflowConfig(
                path=Path("<shell>"),
                top=self.top,
                netlist=self.source,
                cell_lib=self.profile.cell_map,
                liberty=self.profile.liberty,
                verilog_models=self.profile.verilog_models,
                yosys_ver="",
                fault_model=FaultModelConfig(),
                simulation=SimulationConfig(),
                atpg=AtpgConfig(),
                report=ReportConfig(output=Path("coverage.rpt")),
                scan=ScanConfig(run_techmap=False),
                output_root=self.output_root,
                clocks=tuple(self.declared_clocks),
                blackbox_instances=tuple(self.declared_blackbox),
                test_mode=self.test_mode,
            )
        for key, value in self.options.items():
            if key == "atpg.max_rounds":
                cfg = replace(cfg, atpg=replace(cfg.atpg, max_rounds=int(value)))
            elif key == "atpg.sat_timeout_seconds":
                cfg = replace(
                    cfg,
                    atpg=replace(cfg.atpg, sat_timeout_seconds=int(value)),
                )
            elif key == "atpg.workers":
                cfg = replace(cfg, atpg=replace(cfg.atpg, workers=int(value)))
            elif key == "atpg.easy_fault_reserve":
                cfg = replace(
                    cfg, atpg=replace(cfg.atpg, easy_fault_reserve=int(value))
                )
            elif key == "atpg.sat_timeout_schedule":
                cfg = replace(
                    cfg,
                    atpg=replace(cfg.atpg, sat_timeout_schedule=value),
                )
            elif key == "atpg.preflight":
                cfg = replace(
                    cfg,
                    atpg=replace(cfg.atpg, preflight=parse_bool_value(value, key)),
                )
            elif key == "report.threshold":
                cfg = replace(cfg, report=replace(cfg.report, threshold=float(value)))
            elif key == "simulation.unsupported_cells":
                if value not in {"fail", "blackbox"}:
                    raise ShellError(
                        "unsupported_cells must be fail or blackbox",
                        "CONFIG",
                        "INVALID_VALUE",
                    )
                cfg = replace(
                    cfg,
                    simulation=replace(cfg.simulation, unsupported_cells=value),
                )
        return cfg

    def synthesize(self) -> OperationResult:
        cfg = self.materialize_config()
        result = self.service.synthesize(cfg)
        self.synthesized = True
        self.scan_inserted = False
        self.scan_checked = False
        # Switch the active design to the synthesized Yosys JSON so subsequent
        # commands (check_cells, add_scan, run_atpg, write_netlist, ...) operate
        # on it directly -- the user does not have to locate the output path.
        netlist_json = result.artifacts.get("netlist_json")
        if netlist_json is not None:
            self.source = Path(netlist_json)
            self.source_kind = "yosys_json"
        self.checkpoint()
        self._refresh_report()
        return result

    def load_json(self, path: Path, top: str) -> OperationResult:
        """Load an already-synthesized Yosys JSON netlist directly (no synth).

        Thin JSON-only wrapper over read_netlist: rejects Verilog with a clear
        message so the two entry points stay distinct. Mainly for resuming from a
        previously synthesized netlist -- after ``synth`` the synthesized JSON is
        loaded automatically.
        """
        if path.suffix.lower() != ".json":
            raise ShellError(
                "load_json expects a Yosys JSON netlist; "
                "use read_netlist for Verilog",
                "INPUT",
                "UNSUPPORTED_EXTENSION",
            )
        return self.read_netlist(path, top)

    def wrap(
        self,
        *,
        wbr_model: str = "scan",
        clock: str = "clk",
        scan_enable: str = "wbr_se",
        scan_in: str = "wbr_si",
        scan_out: str = "wbr_so",
        targets: list[str] | None = None,
        output: Path | None = None,
    ) -> OperationResult:
        """Inject IEEE 1500 WBR cells onto the boundary ports of the current netlist.

        Saves the wrapped JSON next to the source (or to ``output`` if given),
        then updates the session to point at the wrapped file so subsequent
        add_scan / run_atpg commands operate on the wrapped netlist.
        """
        if self.top is None or self.source is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        if self.source_kind != "yosys_json":
            raise precondition(
                "wrap needs a synthesized Yosys JSON; run synth first",
                "SYNTH_REQUIRED",
            )
        import json as _json

        from faultflow.wrap.ports import WrapError, wrap_ports

        src = _json.loads(self.source.read_text(encoding="utf-8"))
        try:
            dst = wrap_ports(
                src,
                self.top,
                wbr_model=wbr_model,
                clock=clock,
                scan_in=scan_in,
                scan_out=scan_out,
                scan_enable=scan_enable,
                targets=targets,
            )
        except WrapError as exc:
            raise ShellError(str(exc), "CONFIG", "WRAP_FAILED") from exc

        if output is None:
            out_dir = self.source.parent
            output = out_dir / f"{self.top}_wrapped.json"

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json.dumps(dst, indent=2) + "\n", encoding="utf-8")

        modules = dst.get("modules", {})
        mod = modules.get(self.top, {})
        cells = mod.get("cells", {})
        n_wbr_in = sum(
            1 for c in cells.values() if str(c.get("type", "")).startswith("$wbc_in")
        )
        n_wbr_out = sum(
            1 for c in cells.values() if str(c.get("type", "")).startswith("$wbc_out")
        )

        self.source = output
        self.source_kind = "yosys_json"
        self.scan_inserted = False
        self.scan_checked = False
        self.scan_campaign_stale = False
        self.checkpoint()

        msg = (
            f"wrapped {n_wbr_in} wbc_in + {n_wbr_out} wbc_out "
            f"({wbr_model} model) -> {output}"
        )
        return OperationResult(
            "wrap",
            self.top,
            msg,
            artifacts={"wrapped_netlist": output},
        )

    def add_scan(self, **options: object) -> OperationResult:
        if not self.synthesized:
            raise precondition("run synth first", "SYNTH_REQUIRED")
        cfg = self.materialize_config()
        result = self.service.insert_scan(cfg, **options)
        if bool(options.get("dry_run", False)):
            return result
        self.scan_inserted = True
        self.scan_checked = False
        if hasattr(self.service, "has_campaign"):
            self.scan_campaign_stale = bool(self.service.has_campaign(cfg, "scan"))
        self.checkpoint()
        self._refresh_report()
        return result

    def check_scan(self, **options: object) -> OperationResult:
        if not self.scan_inserted:
            raise precondition("run add_scan first", "SCAN_REQUIRED")
        try:
            result = self.service.check_scan(self.materialize_config(), **options)
        except Exception as exc:
            self._refresh_report()
            raise ShellError(str(exc), "RUNNER", "SCAN_CHECK_FAILED") from exc
        self.scan_checked = True
        self.checkpoint()
        self._refresh_report()
        return result

    def run_atpg(self, *, scan: bool = False, **options: object) -> OperationResult:
        if self.top is None:
            raise precondition("run read_netlist first", "NO_DESIGN")
        if not self.synthesized:
            raise precondition("run synth first", "SYNTH_REQUIRED")
        if scan and not self.scan_inserted:
            raise precondition("run add_scan first", "SCAN_REQUIRED")
        if scan and not self.scan_checked:
            raise precondition("run check_scan first", "SCAN_CHECK_REQUIRED")
        if scan and self.scan_campaign_stale:
            raise precondition(
                "scan campaign belongs to a previous scan configuration; "
                "run clean before ATPG. This also clears any combinational "
                "campaign; rerun run_atpg -sa if comparison is needed.",
                "CAMPAIGN_STALE",
            )
        if scan and not self._scan_check_is_fresh():
            self.scan_checked = False
            raise precondition(
                "scan check does not match the current scanned netlist",
                "SCAN_CHECK_STALE",
            )
        if bool(options.pop("serial_ref", False)):
            return self.service.serial_reference(self.materialize_config(), scan=scan)
        result = self.service.run_atpg(self.materialize_config(), scan=scan, **options)
        self._refresh_report()
        return result

    def status(self, *, scan: bool = False) -> OperationResult:
        return self.service.status(self.materialize_config(), scan=scan)

    def report(self) -> OperationResult:
        return self.service.write_report(self.materialize_config())

    def clean(self) -> OperationResult:
        result = self.service.clean_campaigns(self.materialize_config())
        self.scan_campaign_stale = False
        self.checkpoint()
        self._refresh_report()
        return result

    def write_netlist(
        self,
        *,
        scan: bool = False,
        techmap: bool = False,
        verify: bool = False,
        output: Path | None = None,
    ) -> OperationResult:
        if verify:
            raise unsupported(
                "techmap verification is not implemented",
                "TECHMAP_VERIFICATION",
            )
        if scan and not self.scan_inserted:
            raise precondition("run add_scan first", "SCAN_REQUIRED")
        if (
            scan
            and techmap
            and (self.profile is None or not self.profile.supports_physical_scan)
        ):
            raise unsupported(
                "selected PDK has no physical scan-cell binding",
                "PHYSICAL_SCAN_BINDING",
            )
        return self.service.write_netlist(
            self.materialize_config(),
            scan=scan,
            techmap=techmap,
            output=output,
        )

    def set_option(self, key: str, value: str) -> OperationResult:
        allowed = {
            "atpg.easy_fault_reserve",
            "atpg.max_rounds",
            "atpg.preflight",
            "atpg.sat_timeout_seconds",
            "atpg.sat_timeout_schedule",
            "atpg.workers",
            "report.threshold",
            "simulation.unsupported_cells",
        }
        if key not in allowed:
            raise ShellError(f"unsupported option: {key}", "CONFIG", "INVALID_OPTION")
        if key in {"atpg.max_rounds", "atpg.sat_timeout_seconds", "atpg.workers"}:
            try:
                if int(value) < 1:
                    raise ValueError
            except ValueError:
                raise ShellError(
                    f"{key} must be a positive integer", "CONFIG", "INVALID_VALUE"
                )
        if key == "atpg.easy_fault_reserve":
            try:
                if int(value) < 0:
                    raise ValueError
            except ValueError:
                raise ShellError(
                    "atpg.easy_fault_reserve must be a non-negative integer",
                    "CONFIG",
                    "INVALID_VALUE",
                )
        if key == "atpg.sat_timeout_schedule":
            try:
                parse_timeout_schedule(value, 1)
            except ConfigError as exc:
                raise ShellError(str(exc), "CONFIG", "INVALID_VALUE")
        if key == "atpg.preflight":
            try:
                parse_bool_value(value, key)
            except ConfigError as exc:
                raise ShellError(str(exc), "CONFIG", "INVALID_VALUE")
        self.options[key] = value
        if self.top is not None:
            self.checkpoint()
        return OperationResult("set_option", self.top or "", f"{key}={value}")

    def unset_option(self, key: str) -> OperationResult:
        self.options.pop(key, None)
        if self.top is not None:
            self.checkpoint()
        return OperationResult("unset_option", self.top or "", key)

    def save_session(self) -> OperationResult:
        path, digest = self.checkpoint()
        return OperationResult(
            "save_session",
            self.top or "",
            f"session saved: {path}",
            artifacts={"session": path},
            metrics={"sha256": digest},
        )

    def load_session(self, top: str, *, resume: bool = False) -> OperationResult:
        if self.top is not None:
            raise precondition("reset the current design first", "NO_DESIGN")
        path = self.output_root / top / ".faultflow" / "session.json"
        if not path.exists():
            raise ShellError(
                f"session not found: {path}",
                "PRECONDITION",
                "CAMPAIGN_MISSING",
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        source = Path(str(data["source"]))
        if not source.exists():
            raise ShellError(f"netlist not found: {source}", "INPUT", "FILE_NOT_FOUND")
        profile = get_profile(str(data["profile"]))
        self.top = top
        self.source = source
        self.source_kind = str(data["source_kind"])
        self.profile = profile
        self.synthesized = (
            bool(data["synthesized"]) if resume else self.source_kind == "yosys_json"
        )
        self.scan_inserted = bool(data["scan_inserted"]) if resume else False
        self.scan_checked = bool(data["scan_checked"]) if resume else False
        self.scan_campaign_stale = (
            bool(data.get("scan_campaign_stale", False)) if resume else False
        )
        self.options = dict(data.get("options", ()))
        self.declared_clocks = [
            ClockSpec(port=str(row[0]), off_state=int(row[1]))
            for row in data.get("declared_clocks", [])
        ]
        self.declared_blackbox = [
            str(name) for name in data.get("declared_blackbox", [])
        ]
        self.test_mode = str(data.get("test_mode", "functional"))
        if resume and self.scan_inserted:
            cfg = self.materialize_config()
            if not cfg.scan_manifest_path.exists():
                self.scan_inserted = False
                self.scan_checked = False
            elif self.scan_checked and not self._scan_check_is_fresh():
                self.scan_checked = False
        return OperationResult(
            "resume" if resume else "load_session",
            top,
            f"session {'resumed' if resume else 'loaded'}: {top}",
        )

    def _refresh_report(self) -> None:
        if (
            self.top is not None
            and self.profile is not None
            and hasattr(self.service, "write_report")
        ):
            self.service.write_report(self.materialize_config())

    def _scan_check_is_fresh(self) -> bool:
        try:
            cfg = self.materialize_config()
            manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
            latest = manifest.get("latest_check")
            generic = Path(str(manifest["generic_json"]))
            if not isinstance(latest, dict) or latest.get("status") != "PASS":
                return False
            digest = hashlib.sha256(generic.read_bytes()).hexdigest()
            return latest.get("generic_json_hash") == digest
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def reset(self) -> None:
        self.top = None
        self.source = None
        self.source_kind = None
        self.profile = None
        self.synthesized = False
        self.scan_inserted = False
        self.scan_checked = False
        self.scan_campaign_stale = False
        self.options.clear()
        self.declared_clocks.clear()
        self.declared_blackbox.clear()
        self.test_mode = "functional"
