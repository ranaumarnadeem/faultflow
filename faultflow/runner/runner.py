from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from faultflow.atpg import (
    PatternError,
    VectorSet,
    parse_bench_inputs,
    parse_bench_outputs,
    parse_quaigh_test,
)
from faultflow.config import (
    FaultflowConfig,
    LEGACY_SCAN_DB_NAME,
    benchmark_synth_bench,
    benchmark_synth_gate_verilog,
    benchmark_synth_json,
    benchmark_synth_test,
)
from faultflow.db import (
    CAMPAIGN_TYPE_COMB,
    CAMPAIGN_TYPE_SCAN,
    CAMPAIGN_TYPE_SCAN_EXTEST,
    SchemaError,
    abort_pending_candidates,
    connect,
    ensure_campaign,
    init_schema,
    latest_campaign_id,
    summary,
)
from faultflow.reporter import write_reports
from faultflow.scan import (
    ScanError,
    plan_scan_json,
    stitch_scan_json,
    write_scan_techmap,
    run_scan_techmap,
    run_scan_techmap_json,
    verilog_to_json,
)
from faultflow.rule_check.model import RuleCheckReport
from faultflow.scan.checks import check_scan_structure
from faultflow.scan.atpg_view import build_scan_atpg_view
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.manifest import manifest_clock_net_ids
from faultflow.scan.reports import (
    format_dry_run,
    hash_file,
    load_manifest,
    manifest_from_result,
    utc_timestamp,
    write_scan_artifacts,
)
from faultflow.verify import IverilogVerifier, VerificationError

log = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    pass


class FingerprintMismatchError(RunnerError):
    pass


TRANSIENT_NAMES = {
    "__pycache__",
    ".pytest_cache",
    "parser.out",
    "parsetab.py",
}


YOSYS_TEMPLATE = """read_verilog {verilog}
hierarchy -check -top {top}
proc
flatten
opt_expr
opt_clean
synth -top {top}
dfflibmap -liberty {liberty}
abc -liberty {liberty}
delete t:$scopeinfo
clean
write_json {json}
write_verilog {gate_verilog}
"""


def _hash_file(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_json(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def _truthy_attr(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if set(text) <= {"0", "1"}:
        return "1" in text
    return False


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RunnerError(f"JSON root must be an object: {path}")
    return data


def _json_top_module(path: Path, top: str) -> tuple[str, dict[str, Any]]:
    data = _load_json_object(path)
    modules = data.get("modules")
    if not isinstance(modules, dict):
        raise RunnerError(f"Yosys JSON missing modules: {path}")
    if top in modules and isinstance(modules[top], dict):
        return top, modules[top]
    for name, module in modules.items():
        if not isinstance(module, dict):
            continue
        attrs = module.get("attributes", {})
        if isinstance(attrs, dict) and _truthy_attr(attrs.get("top", False)):
            return str(name), module
    raise RunnerError(f"Cannot find top module {top} in {path}")


def _expand_bus_bits(
    port_name: str, bits: list[int], netnames: dict[str, Any]
) -> list[str]:
    """Expand a multi-bit port to individual bit-indexed names via netnames.

    Tries portname[N] entries in netnames, matching each bit by net ID.
    Falls back to [port_name] (first-bit only) if any bit can't be resolved.
    """
    prefix = port_name + "["
    bit_id_to_indexed: dict[int, str] = {}
    for nn_name, nn_val in netnames.items():
        if not nn_name.startswith(prefix) or not nn_name.endswith("]"):
            continue
        if not isinstance(nn_val, dict):
            continue
        nn_bits = nn_val.get("bits", [])
        if (
            isinstance(nn_bits, list)
            and len(nn_bits) == 1
            and isinstance(nn_bits[0], int)
        ):
            bit_id_to_indexed[nn_bits[0]] = nn_name
    result = []
    for bit_id in bits:
        if bit_id not in bit_id_to_indexed:
            return [port_name]
        result.append(bit_id_to_indexed[bit_id])
    return result if result else [port_name]


def _port_names(
    path: Path, top: str, direction: str, expand_buses: bool = False
) -> list[str]:
    _, module = _json_top_module(path, top)
    ports = module.get("ports")
    if not isinstance(ports, dict):
        raise RunnerError(f"module {top} ports must be an object")
    raw_netnames = module.get("netnames", {}) if expand_buses else {}
    netnames: dict[str, Any] = raw_netnames if isinstance(raw_netnames, dict) else {}
    out: list[str] = []
    for name, port in ports.items():
        if isinstance(port, dict) and port.get("direction") == direction:
            bits = port.get("bits")
            if not isinstance(bits, list) or not any(isinstance(b, int) for b in bits):
                continue
            if expand_buses and len(bits) > 1:
                int_bits = [b for b in bits if isinstance(b, int)]
                out.extend(_expand_bus_bits(str(name), int_bits, netnames))
            else:
                out.append(str(name))
    return sorted(out)


def _port_name_for_net(path: Path, top: str, net_id: int, direction: str) -> str | None:
    _, module = _json_top_module(path, top)
    ports = module.get("ports")
    if not isinstance(ports, dict):
        return None
    for name, port in ports.items():
        if not isinstance(port, dict) or port.get("direction") != direction:
            continue
        bits = port.get("bits")
        if isinstance(bits, list) and bits == [net_id]:
            return str(name)
    return None


def _load_core() -> Any | None:
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "build/src/core",
        repo_root / "build",
        repo_root,
        Path("build/src/core"),
        Path("build"),
        Path("."),
    ]
    old_path = list(sys.path)
    try:
        for candidate in candidates:
            if candidate.exists():
                sys.path.insert(0, str(candidate.resolve()))
        try:
            import _faultflow_core  # type: ignore[import-not-found]

            return _faultflow_core
        except ImportError:
            return None
    finally:
        sys.path[:] = old_path


class Runner:
    def __init__(self, cfg: FaultflowConfig):
        self.cfg = cfg

    def _conn(self, scan: bool = False) -> sqlite3.Connection:
        del scan  # unified DB; campaign_type selects scope
        conn = connect(self.cfg.db_path)
        init_schema(conn)
        return conn

    @contextmanager
    def _db(self, scan: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._conn(scan=scan)
        try:
            yield conn
        finally:
            conn.close()

    def _campaign_type(self, scan: bool = False) -> str:
        return CAMPAIGN_TYPE_SCAN if scan else CAMPAIGN_TYPE_COMB

    def _ensure_campaign(
        self, conn: sqlite3.Connection, fp: dict[str, object], *, scan: bool = False
    ) -> int:
        payload = dict(fp)
        if scan:
            payload.setdefault("manifest_hash", "")
            payload.setdefault("atpg_view_schema_ver", "")
        return ensure_campaign(conn, self._campaign_type(scan), payload)

    def _config_fingerprint_payload(self) -> dict[str, object]:
        return {
            "top": self.cfg.top,
            "cell_lib": str(self.cfg.cell_lib),
            "fault_model": self.cfg.fault_model.model,
            "launch": self.cfg.fault_model.launch,
            "collapsing": self.cfg.fault_model.collapsing,
            "unsupported_cells": self.cfg.simulation.unsupported_cells,
            "include_clock_faults": self.cfg.fault_model.include_clock_faults,
            "include_reset_faults": self.cfg.fault_model.include_reset_faults,
            "atpg_tool": self.cfg.atpg.tool,
            "atpg_mode": self.cfg.atpg.mode,
            # Blackboxing changes normalization (boundary nets) and therefore the
            # fault set, so a change must invalidate resume via config_hash.
            "blackbox_instances": list(self.cfg.blackbox_instances),
            # IEEE 1500 test mode reconfigures the observable/control point sets,
            # so INTEST and EXTEST are distinct runs from FUNCTIONAL.
            "test_mode": self.cfg.test_mode,
            # The native shiftable WBR model adds the boundary register as a scan
            # chain (new control/observe points + fault sites), so buffer<->scan is
            # a distinct campaign.
            "wbr_model": self.cfg.wbr_model,
        }

    def _rendered_yosys_script(self, source: Path | None = None) -> str:
        src = source or self._existing_verilog_source()
        return YOSYS_TEMPLATE.format(
            verilog=src if src is not None else self.cfg.netlist,
            top=self.cfg.top,
            liberty=self.cfg.liberty or "",
            json=self.cfg.intermediate_dir / f"{self.cfg.top}.json",
            gate_verilog=self.cfg.intermediate_dir / f"{self.cfg.top}_gate.v",
        )

    def _existing_verilog_source(self) -> Path | None:
        candidates = []
        if self.cfg.netlist.suffix == ".v":
            candidates.append(self.cfg.netlist)
        candidates.extend(
            [
                Path("tests/benchmarks/iscas85") / f"{self.cfg.top}.v",
                Path("tests/benchmarks/iscas89") / f"{self.cfg.top}.v",
            ]
        )
        for path in candidates:
            if path.exists():
                return path
        return None

    def _extract_yosys_version(self) -> str:
        log = self.cfg.logs_dir / "yosys.log"
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^Yosys\s+(.+)$", line.strip())
                if match:
                    return match.group(1).strip()
        return self.cfg.yosys_ver

    def _fingerprint(self, netlist: Path | None = None) -> dict[str, object]:
        netlist_path = netlist if netlist is not None else self.cfg.netlist
        config_hash = _hash_text(
            json.dumps(self._config_fingerprint_payload(), sort_keys=True)
        )
        return {
            "top": self.cfg.top,
            "netlist_hash": _hash_file(netlist_path),
            "cell_lib_hash": _hash_file(self.cfg.cell_lib),
            "config_hash": config_hash,
            "template_hash": _hash_text(self._rendered_yosys_script()),
            "yosys_version": self._extract_yosys_version(),
            "faultflow_version": "pipeline-v1",
            "collapsing": int(self.cfg.fault_model.collapsing),
            "unsupported_cells": self.cfg.simulation.unsupported_cells,
            "include_clock_faults": int(self.cfg.fault_model.include_clock_faults),
            "include_reset_faults": int(self.cfg.fault_model.include_reset_faults),
            "fault_model": self.cfg.fault_model.model,
            "blackbox_instances": list(self.cfg.blackbox_instances),
            "test_mode": self.cfg.test_mode,
            "wbr_model": self.cfg.wbr_model,
        }

    def _stored_fingerprint(
        self, conn: sqlite3.Connection, *, scan: bool = False
    ) -> dict[str, object] | None:
        campaign_id = latest_campaign_id(conn, self._campaign_type(scan))
        if campaign_id is None:
            return None
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _check_fingerprint(
        self,
        conn: sqlite3.Connection,
        current: dict[str, object],
        *,
        scan: bool = False,
    ) -> int:
        try:
            return self._ensure_campaign(conn, current, scan=scan)
        except SchemaError as exc:
            raise FingerprintMismatchError(str(exc)) from exc

    def init(self) -> str:
        self.cfg.ensure_workspace()
        with self._db() as conn:
            existing_netlist = self._existing_json_netlist()
            if existing_netlist is not None:
                fp = self._fingerprint(existing_netlist)
                self._check_fingerprint(conn, fp)
        return f"initialized {self.cfg.output_dir}"

    def _json_netlist_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if _is_json(self.cfg.netlist):
            candidates.append(self.cfg.netlist)
        candidates.extend(benchmark_synth_json(self.cfg.top))
        candidates.append(self.cfg.intermediate_dir / f"{self.cfg.top}.json")
        return candidates

    def _existing_json_netlist(self) -> Path | None:
        for path in self._json_netlist_candidates():
            if path.exists():
                return path
        return None

    def _find_netlist(self) -> Path:
        if self.cfg.netlist.suffix in {".v", ".sv"}:
            return self._run_yosys()
        if (
            not self.cfg.netlist.exists()
            and self._existing_verilog_source() is not None
        ):
            return self._run_yosys()
        candidates = self._json_netlist_candidates()
        for path in candidates:
            if path.exists():
                return path
        return self._run_yosys()

    def find_netlist(self) -> Path:
        return self._find_netlist()

    def synth(self) -> Path:
        t0 = time.perf_counter()
        log.info("synth  running Yosys (top=%s) ...", self.cfg.top)
        result = self._run_yosys()
        stat = self._yosys_stat(result)
        log.info("synth  complete  %s  %.1fs", stat, time.perf_counter() - t0)
        return result

    def _find_verilog_source(self) -> Path:
        if self.cfg.netlist.suffix == ".sv":
            raise RunnerError("SystemVerilog (.sv) is not supported yet")
        existing = self._existing_verilog_source()
        if existing is not None:
            return existing
        raise RunnerError(
            f"Cannot find JSON netlist or Verilog source for top {self.cfg.top}"
        )

    def _run_yosys(self) -> Path:
        if self.cfg.liberty is None or not self.cfg.liberty.exists():
            raise RunnerError("Yosys generation requires an existing liberty file")
        self.cfg.ensure_workspace()
        source = self._find_verilog_source()
        json_path = self.cfg.intermediate_dir / f"{self.cfg.top}.json"
        gate_v = self.cfg.intermediate_dir / f"{self.cfg.top}_gate.v"
        script = self.cfg.generated_scripts_dir / "yosys_synth.tcl"
        script.write_text(
            YOSYS_TEMPLATE.format(
                verilog=source,
                top=self.cfg.top,
                liberty=self.cfg.liberty,
                json=json_path,
                gate_verilog=gate_v,
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["yosys", "-s", str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.cfg.logs_dir / "yosys.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(f"Yosys failed; see {self.cfg.logs_dir / 'yosys.log'}")
        return json_path

    def _yosys_stat(self, path: Path, *, from_verilog: bool = False) -> str:
        """Run yosys stat and return a one-line summary, e.g. '47 cells  285.6 µm²'."""
        read_cmd = f"read_verilog {path}" if from_verilog else f"read_json {path}"
        stat_cmd = (
            f"stat -liberty {self.cfg.liberty}"
            if not from_verilog
            and self.cfg.liberty is not None
            and self.cfg.liberty.exists()
            else "stat"
        )
        proc = subprocess.run(
            ["yosys", "-Q", "-p", f"{read_cmd}; {stat_cmd}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        cells = area = None
        for line in proc.stdout.splitlines():
            # Plain stat (no liberty): "   Number of cells:             47"
            if "Number of cells:" in line:
                cells = line.split()[-1]
            # Liberty stat table row ending with " cells":
            # "       12  110.106 cells"
            elif line.strip().endswith(" cells"):
                parts_l = line.split()
                if len(parts_l) >= 2 and parts_l[0].isdigit():
                    cells = parts_l[0]
                    if len(parts_l) >= 3:
                        area = parts_l[1]
            if "Chip area for module" in line:
                area = line.split()[-1]
        parts = [f"{cells} cells"] if cells else []
        if area:
            parts.append(f"{area} µm²")
        return "  ".join(parts) if parts else "(stat unavailable)"

    def _scan_manifest_path(self) -> Path:
        return self.cfg.scan_manifest_path

    def scan(
        self,
        run_techmap: bool | None = None,
        scan_chains: int | None = None,
        max_chain_length: int | None = None,
        scan_in: str | None = None,
        scan_out: str | None = None,
        scan_enable: str | None = None,
        dry_run: bool = False,
        generic_json_path: Path | None = None,
        scan_report_path: Path | None = None,
    ) -> str:
        self.cfg.ensure_workspace()
        netlist = self._find_netlist()
        chains = self.cfg.scan.chains if scan_chains is None else scan_chains
        max_len = (
            self.cfg.scan.max_chain_length
            if max_chain_length is None
            else max_chain_length
        )
        si_base = self.cfg.scan.scan_in if scan_in is None else scan_in
        so_base = self.cfg.scan.scan_out if scan_out is None else scan_out
        se_name = self.cfg.scan.scan_enable if scan_enable is None else scan_enable
        do_techmap = self.cfg.scan.run_techmap if run_techmap is None else run_techmap

        if dry_run:
            try:
                plan = plan_scan_json(
                    netlist_json=netlist,
                    cell_map_json=self.cfg.cell_lib,
                    top=self.cfg.top,
                    scan_chains=chains,
                    max_chain_length=max_len,
                    scan_in_base=si_base,
                    scan_out_base=so_base,
                    scan_enable=se_name,
                )
            except ScanError as exc:
                raise RunnerError(str(exc)) from exc
            return format_dry_run(plan)

        generic_json = generic_json_path or self.cfg.scan_json_path
        techmap_v = self.cfg.generated_scripts_dir / "faultflow_scanff_map.v"
        sky130_v = self.cfg.scan_verilog_path
        try:
            t_stitch = time.perf_counter()
            log.info("scan   stitching %d chains (top=%s) ...", chains, self.cfg.top)
            result = stitch_scan_json(
                netlist_json=netlist,
                cell_map_json=self.cfg.cell_lib,
                top=self.cfg.top,
                output_json=generic_json,
                scan_chains=chains,
                max_chain_length=max_len,
                scan_in_base=si_base,
                scan_out_base=so_base,
                scan_enable=se_name,
            )
            log.info(
                "scan   inserted  %d chains  %d scan cells  %.1fs",
                result.chain_count,
                result.cell_count,
                time.perf_counter() - t_stitch,
            )
            write_scan_techmap(techmap_v)
            techmapped: Path | None = None
            if do_techmap:
                t_tech = time.perf_counter()
                log.info("scan   techmapping to Sky130 ...")
                techmapped = run_scan_techmap(
                    generic_json=generic_json,
                    techmap_verilog=techmap_v,
                    output_verilog=sky130_v,
                    top=result.top,
                    log_path=self.cfg.logs_dir / "yosys_scan.log",
                    script_path=self.cfg.generated_scripts_dir / "yosys_scan.ys",
                )
                stat = self._yosys_stat(techmapped, from_verilog=True)
                log.info(
                    "scan   techmap complete  %s  %.1fs",
                    stat,
                    time.perf_counter() - t_tech,
                )
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc

        manifest = manifest_from_result(result, netlist, techmap_v, techmapped)
        write_scan_artifacts(
            self.cfg.manifests_dir,
            scan_report_path or self.cfg.scan_report_path,
            manifest,
        )
        manifest_path = self._scan_manifest_path()
        tech_note = f" sky130={sky130_v}" if do_techmap else " sky130=skipped"
        return (
            f"scan complete top={result.top} chains={result.chain_count} "
            f"cells={result.cell_count} generic={generic_json} "
            f"techmap={techmap_v}{tech_note} manifest={manifest_path}"
        )

    def scan_status(self) -> str:
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            raise RunnerError(f"scan manifest not found: {manifest_path}")
        manifest = load_manifest(manifest_path)
        latest = manifest.get("latest_check")
        check_status = latest.get("status") if isinstance(latest, dict) else "not-run"
        ineligible = manifest.get("ineligible_ffs", [])
        ineligible_count = len(ineligible) if isinstance(ineligible, list) else 0
        return (
            f"top={manifest.get('top')} chains={manifest.get('chain_count')} "
            f"scan_cells={manifest.get('cell_count')} "
            f"ineligible={ineligible_count} "
            f"check={check_status} manifest={manifest_path}"
        )

    def scan_techmap(self) -> str:
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            raise RunnerError(f"scan manifest not found: {manifest_path}")
        manifest = load_manifest(manifest_path)
        generic_json = Path(str(manifest["generic_json"]))
        if _hash_file(generic_json) != str(manifest.get("generic_json_hash", "")):
            raise RunnerError("generic scan JSON hash does not match manifest")
        techmap_v = Path(str(manifest["techmap_verilog"]))
        sky130_v = self.cfg.scan_verilog_path
        write_scan_techmap(techmap_v)
        try:
            t0 = time.perf_counter()
            log.info("techmap  running (top=%s) ...", str(manifest["top"]))
            techmapped = run_scan_techmap(
                generic_json=generic_json,
                techmap_verilog=techmap_v,
                output_verilog=sky130_v,
                top=str(manifest["top"]),
                log_path=self.cfg.logs_dir / "yosys_scan.log",
                script_path=self.cfg.generated_scripts_dir / "yosys_scan.ys",
            )
            stat = self._yosys_stat(techmapped, from_verilog=True)
            log.info("techmap  complete  %s  %.1fs", stat, time.perf_counter() - t0)
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc
        manifest["sky130_verilog"] = str(techmapped)
        write_scan_artifacts(
            self.cfg.manifests_dir,
            self.cfg.scan_report_path,
            manifest,
        )
        return f"scan techmap complete top={manifest['top']} sky130={techmapped}"

    def _scan_vector_source(
        self,
        source_json: Path,
        vectors_path: Path | None,
    ) -> tuple[VectorSet, str]:
        input_order = _port_names(source_json, self.cfg.top, "input")
        pattern_order = self._scan_pattern_input_order(source_json, input_order)
        if vectors_path is not None:
            vectors = parse_quaigh_test(
                vectors_path, pattern_order, source=str(vectors_path)
            )
            return vectors, str(vectors_path)

        candidates = [
            self.cfg.patterns_path,
            *benchmark_synth_test(self.cfg.top),
        ]
        for path in candidates:
            if path.exists():
                try:
                    vectors = parse_quaigh_test(path, pattern_order, source=str(path))
                except PatternError:
                    continue
                return vectors, str(path)

        try:
            generated = self._find_vectors()
            _, generated_order = self._find_order_sidecar()
            vectors = parse_quaigh_test(
                generated, generated_order, source=str(generated)
            )
            return vectors, str(generated)
        except (PatternError, RunnerError):
            # Need at least max_chain_length + 2 cycles to shift through every
            # cell in the longest chain. Using total cell count was 4-8x too many.
            cycle_count = max(10, self._max_scan_chain_length() + 2)
            smoke_vectors: list[dict[str, bool]] = []
            for cycle_index in range(cycle_count):
                smoke_vectors.append(
                    {
                        name: ((cycle_index + pi_index) % 2) == 1
                        for pi_index, name in enumerate(input_order)
                    }
                )
            return (
                VectorSet("deterministic_scan_smoke", input_order, smoke_vectors),
                "smoke",
            )

    def _scan_pattern_input_order(
        self,
        source_json: Path,
        fallback_order: list[str],
    ) -> list[str]:
        input_names = set(_port_names(source_json, self.cfg.top, "input"))
        candidates = [
            self.cfg.intermediate_dir / f"{self.cfg.top}.bench",
            *benchmark_synth_bench(self.cfg.top),
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                order = parse_bench_inputs(path)
            except PatternError:
                continue
            if set(order) <= input_names:
                return order
        return fallback_order

    def _current_scan_cell_count(self) -> int:
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            return 0
        manifest = load_manifest(manifest_path)
        value = manifest.get("cell_count", 0)
        return int(value) if isinstance(value, int) else 0

    def _max_scan_chain_length(self) -> int:
        """Return the length of the longest scan chain (not the total cell count).
        Used to compute the minimum number of smoke-test cycles needed to shift
        through every cell in the longest chain exactly once."""
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            return self._current_scan_cell_count()
        manifest = load_manifest(manifest_path)
        chains = manifest.get("chains")
        if not isinstance(chains, list) or not chains:
            return self._current_scan_cell_count()
        return max(
            len(c.get("cell_records", [])) for c in chains if isinstance(c, dict)
        )

    def _sequence_outputs(
        self,
        netlist: Path,
        vectors: VectorSet,
        output_order: list[str],
        clock_names: list[str],
        extra_inputs: dict[str, bool] | None = None,
        cell_map_path: Path | None = None,
    ) -> list[dict[str, bool]]:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        extra = extra_inputs or {}
        input_order = sorted(set(vectors.input_order) | set(extra) | set(clock_names))
        sequences = []
        for vector in vectors.vectors:
            base = {name: bool(vector.get(name, False)) for name in input_order}
            base.update(extra)
            low = dict(base)
            high = dict(base)
            for clk in clock_names:
                low[clk] = False
                high[clk] = True
            sequences.append([low, high])
        cell_map = cell_map_path if cell_map_path is not None else self.cfg.cell_lib
        return list(
            core.fault_free_sequence_outputs(
                str(netlist),
                str(cell_map),
                sequences,
                input_order,
                output_order,
                self.cfg.simulation.unsupported_cells,
            )
        )

    def _scan_normal_mode_context(
        self,
        manifest: dict[str, object],
        vectors_path: Path | None,
    ) -> tuple[VectorSet, VectorSet, list[str], list[str], dict[str, bool], str]:
        source_json = Path(str(manifest["source_json"]))
        output_order = _port_names(
            source_json, str(manifest["top"]), "output", expand_buses=True
        )
        if not output_order:
            raise RunnerError("normal-mode scan check requires at least one PO")
        clock_net_ids = manifest_clock_net_ids(manifest, error_cls=RunnerError)
        clock_names: list[str] = []
        for clk_net in clock_net_ids:
            port = _port_name_for_net(
                source_json, str(manifest["top"]), clk_net, "input"
            )
            if port is None:
                raise RunnerError(
                    f"cannot map scan clock net {clk_net} to an input port"
                )
            clock_names.append(port)
        vectors, vector_source = self._scan_vector_source(source_json, vectors_path)
        raw_scan_inputs = manifest.get("scan_inputs", [])
        scan_inputs = (
            [str(name) for name in raw_scan_inputs]
            if isinstance(raw_scan_inputs, list)
            else []
        )
        scan_extra = {str(manifest["scan_enable"]): False}
        scan_extra.update({name: False for name in scan_inputs})
        scanned_vectors = VectorSet(
            source=vectors.source,
            input_order=sorted(set(vectors.input_order) | set(scan_extra)),
            vectors=[
                {
                    **{name: vector.get(name, False) for name in vectors.input_order},
                    **scan_extra,
                }
                for vector in vectors.vectors
            ],
        )
        return (
            vectors,
            scanned_vectors,
            output_order,
            clock_names,
            scan_extra,
            vector_source,
        )

    def _run_scan_normal_mode_check(
        self,
        manifest: dict[str, object],
        vectors_path: Path | None,
    ) -> tuple[list[str], dict[str, object]]:
        source_json = Path(str(manifest["source_json"]))
        generic_json = Path(str(manifest["generic_json"]))
        (
            vectors,
            scanned_vectors,
            output_order,
            clock_names,
            scan_extra,
            vector_source,
        ) = self._scan_normal_mode_context(manifest, vectors_path)

        original = self._sequence_outputs(
            source_json, vectors, output_order, clock_names
        )
        scanned = self._sequence_outputs(
            generic_json,
            scanned_vectors,
            output_order,
            clock_names,
            extra_inputs=scan_extra,
            cell_map_path=resolve_scan_cell_map(self.cfg),
        )
        if original != scanned:
            raise RunnerError(
                "normal-mode scanned outputs differ from original outputs"
            )
        return [], {
            "vector_source": vector_source,
            "vector_count": vectors.count,
            "output_order": output_order,
        }

    def _run_scan_techmap_equivalence_check(
        self,
        manifest: dict[str, object],
        scanned_vectors: VectorSet,
        output_order: list[str],
        clock_names: list[str],
        scan_extra: dict[str, bool],
    ) -> dict[str, object]:
        sky = manifest.get("sky130_verilog")
        if not isinstance(sky, str) or not sky:
            raise RunnerError("techmap equivalence check requires sky130_verilog")
        sky130_v = Path(sky)
        if not sky130_v.exists():
            raise RunnerError(f"techmap verilog not found: {sky130_v}")
        generic_json = Path(str(manifest["generic_json"]))
        techmap_v = Path(str(manifest["techmap_verilog"]))
        techmap_json = self.cfg.intermediate_dir / "scan_techmap_check.json"
        try:
            run_scan_techmap_json(
                generic_json,
                techmap_v,
                techmap_json,
                self.cfg.logs_dir / "yosys_scan_check.log",
                self.cfg.generated_scripts_dir / "yosys_scan_check.ys",
            )
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc

        # Re-deriving techmap_json from techmap_verilog (above) only proves the
        # techmap PROCESS is reproducible -- it never reads sky130_v itself, so a
        # sky130_verilog that has drifted from that process (corrupted, hand-edited,
        # written by a stale/different techmap run) would go undetected. Import the
        # actual on-disk artifact and compare its behavior too.
        sky130_json = self.cfg.intermediate_dir / "scan_sky130_check.json"
        try:
            verilog_to_json(
                sky130_v,
                str(manifest["top"]),
                sky130_json,
                self.cfg.logs_dir / "yosys_sky130_check.log",
                self.cfg.generated_scripts_dir / "yosys_sky130_check.ys",
            )
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc

        generic_out = self._sequence_outputs(
            generic_json,
            scanned_vectors,
            output_order,
            clock_names,
            extra_inputs=scan_extra,
            cell_map_path=resolve_scan_cell_map(self.cfg),
        )
        techmap_out = self._sequence_outputs(
            techmap_json,
            scanned_vectors,
            output_order,
            clock_names,
            extra_inputs=scan_extra,
            cell_map_path=self.cfg.cell_lib,
        )
        if generic_out != techmap_out:
            raise RunnerError(
                "normal-mode generic scan outputs differ from techmapped scan outputs"
            )
        sky130_out = self._sequence_outputs(
            sky130_json,
            scanned_vectors,
            output_order,
            clock_names,
            extra_inputs=scan_extra,
            cell_map_path=self.cfg.cell_lib,
        )
        if generic_out != sky130_out:
            raise RunnerError(
                "normal-mode generic scan outputs differ from the on-disk "
                f"sky130_verilog artifact ({sky130_v})"
            )
        return {
            "vector_count": scanned_vectors.count,
            "techmap_json": str(techmap_json),
            "sky130_verilog": str(sky130_v),
            "sky130_json": str(sky130_json),
        }

    def scan_check(
        self,
        vectors_path: Path | None = None,
        require_techmap: bool = False,
        structural_only: bool = False,
    ) -> str:
        t0 = time.perf_counter()
        log.info("check  validating scan chains (top=%s) ...", self.cfg.top)
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            raise RunnerError(f"scan manifest not found: {manifest_path}")
        manifest = load_manifest(manifest_path)
        structural = check_scan_structure(manifest, require_techmap=require_techmap)
        errors = list(structural.errors)
        warnings = list(structural.warnings)
        normal_mode: dict[str, object] | None = None
        techmap_equivalence: dict[str, object] | None = None
        if not errors and not structural_only:
            try:
                extra_warnings, normal_mode = self._run_scan_normal_mode_check(
                    manifest, vectors_path
                )
                warnings.extend(extra_warnings)
            except Exception as exc:
                errors.append(str(exc))

        sky = manifest.get("sky130_verilog")
        run_techmap_check = require_techmap or (
            isinstance(sky, str) and bool(sky) and Path(sky).exists()
        )
        if not errors and run_techmap_check:
            try:
                (
                    _vectors,
                    scanned_vectors,
                    output_order,
                    clock_names,
                    scan_extra,
                    _vector_source,
                ) = self._scan_normal_mode_context(manifest, vectors_path)
                techmap_equivalence = self._run_scan_techmap_equivalence_check(
                    manifest,
                    scanned_vectors,
                    output_order,
                    clock_names,
                    scan_extra,
                )
            except Exception as exc:
                errors.append(str(exc))

        latest_check = {
            "timestamp": utc_timestamp(),
            "status": "PASS" if not errors else "FAIL",
            "warnings": warnings,
            "errors": errors,
            "normal_mode": normal_mode,
            "techmap_equivalence": techmap_equivalence,
            "generic_json_hash": hash_file(Path(str(manifest["generic_json"]))),
        }
        manifest["latest_check"] = latest_check
        write_scan_artifacts(
            self.cfg.manifests_dir,
            self.cfg.scan_report_path,
            manifest,
        )
        if errors:
            log.info(
                "check  FAIL  %d error(s)  %.1fs", len(errors), time.perf_counter() - t0
            )
            raise RunnerError("scan-check failed: " + "; ".join(errors))
        tech_note = " techmap_equiv=PASS" if techmap_equivalence is not None else ""
        log.info("check  PASS%s  %.1fs", tech_note, time.perf_counter() - t0)
        return (
            f"scan-check PASS top={manifest.get('top')} "
            f"vectors={normal_mode.get('vector_count') if normal_mode else 0}"
            f"{tech_note} manifest={manifest_path}"
        )

    def rule_check(self, *, strict: bool = False) -> RuleCheckReport:
        """Run the DFT structural rule set on the synthesized netlist (and the
        scan manifest if present). Standalone: never auto-triggers, never runs
        Yosys; the JSON netlist must already exist."""
        from faultflow.rule_check.report import write_reports as write_rule_reports
        from faultflow.rule_check.rules import run_rule_check

        t0 = time.perf_counter()
        log.info("rule_check  running DFT rules (top=%s) ...", self.cfg.top)
        self.cfg.ensure_workspace()
        netlist = self._existing_json_netlist()
        if netlist is None:
            raise RunnerError(
                "rule_check needs a synthesized JSON netlist; run `sim` or "
                "`synth` first"
            )
        manifest = None
        manifest_path = self._scan_manifest_path()
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
        report = run_rule_check(netlist, self.cfg.cell_lib, self.cfg.top, manifest)
        txt_path = self.cfg.output_dir / "rule_check.rpt"
        json_path = self.cfg.output_dir / "rule_check.json"
        write_rule_reports(report, txt_path, json_path, strict=strict)
        log.info(
            "rule_check  %s  errors=%d warnings=%d  %.1fs",
            "PASS" if report.passed(strict=strict) else "FAIL",
            len(report.errors),
            len(report.warnings),
            time.perf_counter() - t0,
        )
        return report

    def _find_bench_sidecar(self) -> tuple[Path, list[str]]:
        candidates = [
            self.cfg.intermediate_dir / f"{self.cfg.top}.bench",
            *benchmark_synth_bench(self.cfg.top),
        ]
        for path in candidates:
            if path.exists():
                return path, parse_bench_inputs(path)
        return self._run_nl2bench()

    def _run_nl2bench(self) -> tuple[Path, list[str]]:
        gate_v = self.cfg.intermediate_dir / f"{self.cfg.top}_gate.v"
        if not gate_v.exists():
            self._run_yosys()
        if not gate_v.exists():
            raise RunnerError(f"Cannot generate BENCH; missing {gate_v}")
        if self.cfg.liberty is None or not self.cfg.liberty.exists():
            raise RunnerError("BENCH generation requires an existing liberty file")

        bench = self.cfg.intermediate_dir / f"{self.cfg.top}.bench"
        nl2bench = Path("venv/bin/nl2bench")
        tool = str(nl2bench) if nl2bench.exists() else shutil.which("nl2bench")
        if tool is None:
            raise RunnerError(
                "BENCH generation requires nl2bench. "
                "Install it in venv/bin/nl2bench or on PATH."
            )
        cmd = [tool]
        cmd.extend(["-o", str(bench), "-l", str(self.cfg.liberty), str(gate_v)])
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.cfg.logs_dir / "nl2bench.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(
                f"nl2bench failed; see {self.cfg.logs_dir / 'nl2bench.log'}"
            )
        return bench, parse_bench_inputs(bench)

    def _find_order_sidecar(self) -> tuple[Path, list[str]]:
        return self._find_bench_sidecar()

    def _gate_verilog_candidates(self) -> list[Path]:
        return [
            self.cfg.intermediate_dir / f"{self.cfg.top}_gate.v",
            *benchmark_synth_gate_verilog(self.cfg.top),
        ]

    def _find_gate_verilog(self) -> Path:
        for path in self._gate_verilog_candidates():
            if path.exists():
                return path
        if self._existing_verilog_source() is not None:
            self._run_yosys()
            for path in self._gate_verilog_candidates():
                if path.exists():
                    return path
        raise RunnerError(
            f"verification requires generated gate Verilog for top {self.cfg.top}"
        )

    def _find_vectors(self) -> Path:
        candidates = [
            self.cfg.patterns_path,
            *benchmark_synth_test(self.cfg.top),
        ]
        for path in candidates:
            if path.exists():
                return path
        return self._run_quaigh()

    def _run_quaigh(self) -> Path:
        sidecar, _ = self._find_bench_sidecar()
        if sidecar.suffix != ".bench":
            raise RunnerError("Quaigh ATPG input must be .bench")
        output = self.cfg.patterns_path
        self.cfg.ensure_workspace()
        proc = subprocess.run(
            ["quaigh", "atpg", str(sidecar), "-o", str(output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.cfg.logs_dir / "quaigh.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(f"Quaigh failed; see {self.cfg.logs_dir / 'quaigh.log'}")
        return output

    def _purge_transients(self) -> int:
        removed = 0
        if not self.cfg.workspace_dir.exists():
            return removed
        for path in self.cfg.workspace_dir.rglob("*"):
            if path.name not in TRANSIENT_NAMES and path.suffix not in {".pyc", ".pyo"}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        return removed

    def _clean_workspace(self, scan: bool = False) -> int:
        del scan
        removed = 0
        legacy_db_names = ("faultflow.sqlite", LEGACY_SCAN_DB_NAME)
        for name in legacy_db_names:
            base = self.cfg.output_dir / name
            for path in [
                base,
                base.with_name(base.name + "-wal"),
                base.with_name(base.name + "-shm"),
                base.with_name(base.name + "-journal"),
            ]:
                if path.exists():
                    path.unlink()
                    removed += 1
        legacy_scan = self.cfg.output_dir / "scan"
        if legacy_scan.exists():
            shutil.rmtree(legacy_scan, ignore_errors=True)
            removed += 1
        if self.cfg.workspace_dir.exists():
            for child in self.cfg.workspace_dir.iterdir():
                if child.resolve() == self.cfg.manifests_dir.resolve():
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
        self.cfg.ensure_workspace()
        return removed

    def _simulate_with_core(
        self,
        netlist: Path,
        vectors: VectorSet,
        vector_source: str,
        campaign_id: int,
    ) -> dict[str, object]:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        return dict(
            core.simulate_to_db(
                str(netlist),
                str(self.cfg.cell_lib),
                str(self.cfg.db_path),
                campaign_id,
                vectors.vectors,
                vectors.input_order,
                vector_source,
                self.cfg.fault_model.include_clock_faults,
                self.cfg.fault_model.include_reset_faults,
                self.cfg.fault_model.collapsing,
                self.cfg.simulation.unsupported_cells,
                list(self.cfg.blackbox_instances),
                self.cfg.test_mode,
            )
        )

    def _core(self) -> Any:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        return core

    def _external_vectors(self, ext: Path, netlist: Path) -> tuple[VectorSet, Path]:
        if ext.suffix != ".test":
            raise RunnerError("--ext requires a .test file")
        if not ext.exists():
            raise RunnerError(f"external vector file not found: {ext}")
        bench = ext.with_suffix(".bench")
        if not bench.exists():
            raise RunnerError(f"--ext requires BENCH sidecar: {bench}")
        try:
            input_order = parse_bench_inputs(bench)
            netlist_inputs = _port_names(netlist, self.cfg.top, "input")
            if input_order != netlist_inputs:
                raise RunnerError(
                    "external BENCH PI order mismatch: "
                    f"bench={input_order} netlist={netlist_inputs}"
                )
            vectors = parse_quaigh_test(ext, input_order, source=f"external:{ext}")
        except PatternError as exc:
            raise RunnerError(f"invalid external vectors: {exc}") from exc
        return vectors, bench

    def _write_run_timings(
        self,
        run_id: int,
        atpg_seconds: float,
        fault_sim_seconds: float,
        total_seconds: float,
    ) -> None:
        with self._db() as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET atpg_generation_seconds = ?,
                        fault_simulation_seconds = ?,
                        total_sim_seconds = ?
                    WHERE id = ?
                    """,
                    (atpg_seconds, fault_sim_seconds, total_seconds, run_id),
                )

    def _fault_free_outputs_with_core(
        self,
        netlist: Path,
        vectors: VectorSet,
        output_order: list[str],
    ) -> list[dict[str, bool]]:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        return list(
            core.fault_free_outputs(
                str(netlist),
                str(self.cfg.cell_lib),
                vectors.vectors,
                vectors.input_order,
                output_order,
                self.cfg.simulation.unsupported_cells,
            )
        )

    def _fault_free_sequence_outputs_with_core(
        self,
        netlist: Path,
        pairs: list[tuple[dict[str, bool], dict[str, bool]]],
        input_order: list[str],
        output_order: list[str],
    ) -> list[dict[str, bool]]:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        sequences = [[launch, capture] for launch, capture in pairs]
        return list(
            core.fault_free_sequence_outputs(
                str(netlist),
                str(self.cfg.cell_lib),
                sequences,
                input_order,
                output_order,
                self.cfg.simulation.unsupported_cells,
            )
        )

    def _write_verified_vectors(
        self,
        run_id: int,
        vectors: VectorSet,
        expected_outputs: list[dict[str, bool]],
    ) -> None:
        if len(expected_outputs) != vectors.count:
            raise RunnerError(
                f"verified output count {len(expected_outputs)} does not match "
                f"{vectors.count} vectors"
            )
        with self._db() as conn:
            with conn:
                for index, (inputs, expected) in enumerate(
                    zip(vectors.vectors, expected_outputs), 1
                ):
                    cur = conn.execute(
                        """
                        UPDATE vectors
                        SET inputs = ?, expected = ?, verified = 1
                        WHERE run_id = ? AND vector_index = ?
                        """,
                        (
                            json.dumps(inputs, sort_keys=True),
                            json.dumps(expected, sort_keys=True),
                            run_id,
                            index,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise RunnerError(
                            f"missing DB vector row for run_id={run_id} "
                            f"vector_index={index}"
                        )

    def _write_verified_vectors_json(
        self,
        vectors: VectorSet,
        output_order: list[str],
        expected_outputs: list[dict[str, bool]],
    ) -> Path:
        path = self.cfg.intermediate_dir / "verified_vectors.json"
        rows = []
        for index, (inputs, expected) in enumerate(
            zip(vectors.vectors, expected_outputs), 1
        ):
            rows.append(
                {
                    "index": index,
                    "inputs": inputs,
                    "expected": expected,
                    "verified": True,
                }
            )
        payload = {
            "version": 1,
            "top": self.cfg.top,
            "source": vectors.source,
            "input_order": vectors.input_order,
            "output_order": output_order,
            "vectors": rows,
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def _mark_verification_failure(self, error: str) -> None:
        verify_dir = self.cfg.verification_dir
        json_path = verify_dir / "verification_report.json"
        txt_path = verify_dir / "verification_report.txt"
        if json_path.exists():
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        else:
            payload = {
                "version": 1,
                "metadata": {"top": self.cfg.top, "tool": "iverilog"},
                "expected_outputs": [],
                "vector_count": 0,
            }
        payload["passed"] = False
        errors = payload.get("errors", [])
        if not isinstance(errors, list):
            errors = []
        errors.append(error)
        payload["errors"] = errors
        verify_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with txt_path.open("a", encoding="utf-8") as f:
            f.write(f"error: {error}\n")

    def _run_verification(
        self,
        netlist: Path,
        sidecar: Path,
        vectors: VectorSet,
        input_order: list[str],
        *,
        transition: bool = False,
        launch_vectors: list[dict[str, bool]] | None = None,
    ) -> list[dict[str, bool]]:
        if self.cfg.simulation.verify_tool != "iverilog":
            raise RunnerError("Only verify_tool=iverilog is supported")
        if self.cfg.verilog_models is None:
            raise RunnerError("verification requires verilog_models")
        output_order = parse_bench_outputs(sidecar)
        verifier = IverilogVerifier(
            top=self.cfg.top,
            work_dir=self.cfg.verification_dir,
            gate_verilog=self._find_gate_verilog(),
            verilog_models=[self.cfg.verilog_models],
            use_power_pins=self.cfg.simulation.verify_use_power_pins,
        )

        sequential_steps = None
        pairs: list[tuple[dict[str, bool], dict[str, bool]]] = []
        if transition:
            from faultflow.verify.gate import build_transition_sequential_steps

            launches = launch_vectors or []
            pairs = list(zip(launches, vectors.vectors))
            sequential_steps = build_transition_sequential_steps(pairs, input_order)

        try:
            result = verifier.run(input_order, output_order, vectors, sequential_steps)
        except VerificationError as exc:
            self._mark_verification_failure(str(exc))
            raise RunnerError(f"verification failed: {exc}") from exc

        if transition:
            cpp_outputs = self._fault_free_sequence_outputs_with_core(
                netlist, pairs, input_order, output_order
            )
        else:
            cpp_outputs = self._fault_free_outputs_with_core(
                netlist, vectors, output_order
            )
        if cpp_outputs != result.expected_outputs:
            error = "C++ fault-free outputs did not match Iverilog golden outputs"
            mismatch_path = self.cfg.verification_dir / "cpp_crosscheck.txt"
            mismatch_path.parent.mkdir(parents=True, exist_ok=True)
            mismatch_path.write_text(
                error + "\n",
                encoding="utf-8",
            )
            self._mark_verification_failure(error)
            raise RunnerError(
                f"verification failed: C++ fault-free mismatch; see {mismatch_path}"
            )
        self._write_verified_vectors_json(
            vectors, output_order, result.expected_outputs
        )
        return result.expected_outputs

    def _preflight_sim_scan(self) -> dict[str, object]:
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            raise RunnerError(
                f"scan manifest not found: {manifest_path}; run scan first"
            )
        manifest = load_manifest(manifest_path)
        generic_json = Path(str(manifest["generic_json"]))
        if not generic_json.exists():
            raise RunnerError(f"generic scanned JSON not found: {generic_json}")
        actual_hash = hash_file(generic_json)
        expected_hash = str(manifest.get("generic_json_hash", ""))
        if actual_hash != expected_hash:
            raise RunnerError(
                "generic scanned JSON hash does not match scan_manifest.json"
            )
        latest = manifest.get("latest_check")
        if not isinstance(latest, dict) or latest.get("status") != "PASS":
            raise RunnerError(
                "scan-check has not passed; run scan-check before sim --scan"
            )
        check_hash = latest.get("generic_json_hash")
        if check_hash != actual_hash:
            raise RunnerError(
                "scan-check is stale for the current generic scanned JSON; "
                "re-run scan-check"
            )
        ineligible = manifest.get("ineligible_ffs", [])
        if isinstance(ineligible, list) and ineligible:
            # WBR scan cells are deliberately routed to the wrapper chain (fused
            # into the view downstream by fuse_wbr_into_view), NOT the main scan
            # chain -- they are not a full-scan blocker. Genuinely unscannable FFs
            # (unsupported_ff_shape, unknown_cell_type, existing_scan_cell) are.
            blockers = [
                item
                for item in ineligible
                if isinstance(item, dict) and item.get("reason") != "wbr_scan_cell"
            ]
            wbr_count = len(ineligible) - len(blockers)
            if wbr_count:
                log.info(
                    "scan preflight: %d WBR cell(s) routed to wrapper chain",
                    wbr_count,
                )
            if blockers:
                names = ", ".join(
                    sorted(str(item.get("instance", "?")) for item in blockers)
                )
                raise RunnerError(
                    f"sim --scan requires full scan; ineligible FFs remain: {names}"
                )
        return manifest

    def sim(
        self,
        purge: bool = False,
        clean: bool = False,
        verify: bool | None = None,
        ext: Path | None = None,
        max_rounds: int | None = None,
        target_coverage: float | None = None,
        scan: bool = False,
        export_patterns: Path | None = None,
    ) -> str:
        if scan and ext is not None:
            raise RunnerError(
                "External vectors have no scan pseudo-PI semantics; --scan --ext "
                "is not meaningful in this architecture. A scan-aware vector "
                "format would be required."
            )
        if scan:
            # EXTEST tests only the wrapper boundary/interconnect with a dead
            # core, so its fused view is combinational -- it runs through plain
            # native ATPG, not the scan protocol pipeline (see _sim_extest).
            if str(self.cfg.test_mode) == "extest":
                return self._sim_extest(
                    purge=purge,
                    clean=clean,
                    max_rounds=max_rounds,
                    target_coverage=target_coverage,
                )
            return self._sim_scan(
                purge=purge,
                clean=clean,
                max_rounds=max_rounds,
                target_coverage=target_coverage,
                export_patterns=export_patterns,
            )

        total_start = time.perf_counter()
        self.cfg.ensure_workspace()
        cleaned = self._clean_workspace() if clean else 0
        removed = self._purge_transients() if purge else 0
        netlist = self._find_netlist()
        verify_enabled = self.cfg.simulation.verify if verify is None else verify
        # iverilog cannot drive/observe blackbox pseudo-ports, and the blackbox
        # instance has no behavioral model to simulate. Skip the gate rather than
        # mis-verify; the internal GoldenRef/bit-parallel oracle remains the
        # source of truth.
        if verify_enabled and self.cfg.blackbox_instances:
            log.warning(
                "verify  skipped: blackbox instances present (%s); pseudo-ports "
                "are not iverilog-observable",
                ", ".join(self.cfg.blackbox_instances),
            )
            verify_enabled = False
        verified_outputs: list[dict[str, bool]] | None = None
        raw_vector_count: int | None = None

        with self._db() as conn:
            fp = self._fingerprint(netlist)
            campaign_id = self._check_fingerprint(conn, fp)

        if ext is not None:
            vectors, sidecar = self._external_vectors(ext, netlist)
            vector_source = f"external:{ext}"
            atpg_seconds = 0.0
            if verify_enabled:
                verified_outputs = self._run_verification(
                    netlist, sidecar, vectors, vectors.input_order
                )
            sim_start = time.perf_counter()
            sim_result = self._simulate_with_core(
                netlist, vectors, vector_source, campaign_id
            )
            fault_sim_seconds = time.perf_counter() - sim_start
            if verified_outputs is not None:
                raw_run_id = sim_result["run_id"]
                if not isinstance(raw_run_id, (int, str)):
                    raise RunnerError(
                        "C++ simulation result did not include a valid run_id"
                    )
                self._write_verified_vectors(int(raw_run_id), vectors, verified_outputs)
            raw_run_id = sim_result["run_id"]
            if not isinstance(raw_run_id, (int, str)):
                raise RunnerError(
                    "C++ simulation result did not include a valid run_id"
                )
            run_id = int(raw_run_id)
            atpg_terminal = ""
        else:
            from faultflow.runner.progressive_atpg import (
                redundancy_model_id,
                run_progressive_native_atpg,
                run_progressive_transition_atpg,
            )

            transition = self.cfg.fault_model.model == "transition"
            if transition and self.cfg.fault_model.launch == "los":
                raise RunnerError(
                    "launch=los is scan-only; broadside combinational transition "
                    "ATPG has no scan shift. Use `sim --scan` for LOS."
                )
            with self._db() as conn:
                fp = self._fingerprint(netlist)
                model_id = redundancy_model_id(fp)
                campaign_id = self._check_fingerprint(conn, fp)
            if transition:
                log.info(
                    "sim    running progressive transition ATPG (top=%s) ...",
                    self.cfg.top,
                )
                vectors, atpg_stats, run_id, atpg_seconds, fault_sim_seconds = (
                    run_progressive_transition_atpg(
                        self.cfg,
                        netlist,
                        model_id,
                        campaign_id=campaign_id,
                        max_rounds=max_rounds,
                        target_coverage=target_coverage,
                    )
                )
                vector_source = "native_transition_atpg"
            else:
                log.info("sim    running progressive ATPG (top=%s) ...", self.cfg.top)
                vectors, atpg_stats, run_id, atpg_seconds, fault_sim_seconds = (
                    run_progressive_native_atpg(
                        self.cfg,
                        netlist,
                        model_id,
                        campaign_id=campaign_id,
                        max_rounds=max_rounds,
                        target_coverage=target_coverage,
                    )
                )
                vector_source = "native_sat_atpg"
            sidecar = self.cfg.intermediate_dir / vector_source
            atpg_terminal = atpg_stats.terminal_reason
            if self.cfg.atpg.compaction != "none":
                if transition:
                    # Two-frame reverse-sweep over (launch, capture) pairs.
                    from faultflow.runner.compaction import compact_run_transition

                    vectors, run_id, raw_vector_count = compact_run_transition(
                        json_path=str(netlist),
                        cell_map_path=str(self.cfg.cell_lib),
                        db_path=str(self.cfg.db_path),
                        campaign_id=campaign_id,
                        run_id=run_id,
                        vectors=vectors,
                        unsupported=self.cfg.simulation.unsupported_cells,
                    )
                else:
                    from faultflow.runner.compaction import (
                        compact_run,
                        compact_run_dynamic,
                    )

                    if self.cfg.atpg.compaction == "dynamic":
                        vectors, run_id, raw_vector_count = compact_run_dynamic(
                            json_path=str(netlist),
                            cell_map_path=str(self.cfg.cell_lib),
                            db_path=str(self.cfg.db_path),
                            campaign_id=campaign_id,
                            run_id=run_id,
                            vectors=vectors,
                            unsupported=self.cfg.simulation.unsupported_cells,
                            pack_orders=self.cfg.atpg.pack_orders,
                        )
                    else:
                        vectors, run_id, raw_vector_count = compact_run(
                            json_path=str(netlist),
                            cell_map_path=str(self.cfg.cell_lib),
                            db_path=str(self.cfg.db_path),
                            campaign_id=campaign_id,
                            run_id=run_id,
                            vectors=vectors,
                            unsupported=self.cfg.simulation.unsupported_cells,
                        )
                vector_source = vectors.source
            if verify_enabled:
                if transition:
                    # Two-frame launch/capture iverilog replay. Launch frames for
                    # the (possibly compacted) run come from the DB launch_pattern
                    # column, aligned with the capture vectors by vector_index.
                    from faultflow.runner.compaction import _decode

                    with self._db() as conn:
                        launch_rows = conn.execute(
                            "SELECT launch_pattern FROM vectors "
                            "WHERE run_id = ? ORDER BY vector_index",
                            (run_id,),
                        ).fetchall()
                    launches = [
                        _decode(str(row["launch_pattern"]), vectors.input_order)
                        for row in launch_rows
                    ]
                    sidecar, _ = self._find_order_sidecar()
                    verified_outputs = self._run_verification(
                        netlist,
                        sidecar,
                        vectors,
                        vectors.input_order,
                        transition=True,
                        launch_vectors=launches,
                    )
                    self._write_verified_vectors(run_id, vectors, verified_outputs)
                else:
                    sidecar, _ = self._find_order_sidecar()
                    verified_outputs = self._run_verification(
                        netlist, sidecar, vectors, vectors.input_order
                    )
                    self._write_verified_vectors(run_id, vectors, verified_outputs)

        total_seconds = time.perf_counter() - total_start
        self._write_run_timings(run_id, atpg_seconds, fault_sim_seconds, total_seconds)
        mode = "c++"

        with self._db() as conn:
            json_path, txt_path, report = write_reports(
                conn,
                self.cfg,
                campaign_id=campaign_id,
            )

        purge_note = f" purged_transients={removed}" if purge else ""
        clean_note = f" cleaned_db_files={cleaned}" if clean else ""
        terminal_note = f" atpg_terminal={atpg_terminal}" if ext is None else ""
        compaction_note = (
            f" raw_vectors={raw_vector_count}"
            if raw_vector_count is not None and raw_vector_count != vectors.count
            else ""
        )
        log.info(
            "sim    complete  coverage=%.3f%%  vectors=%d  %.1fs",
            report["summary"]["coverage_percent"],
            vectors.count,
            time.perf_counter() - total_start,
        )
        return (
            f"sim complete top={self.cfg.top} mode={mode} "
            f"vectors={vectors.count}{compaction_note} "
            f"source={vector_source} sidecar={sidecar} "
            f"coverage={report['summary']['coverage_percent']:.3f}% "
            f"atpg_seconds={atpg_seconds:.3f} "
            f"fault_sim_seconds={fault_sim_seconds:.3f}{terminal_note} "
            f"report={txt_path} json={json_path}{purge_note}{clean_note}"
        )

    def _sim_scan(
        self,
        *,
        purge: bool = False,
        clean: bool = False,
        max_rounds: int | None = None,
        target_coverage: float | None = None,
        export_patterns: Path | None = None,
    ) -> str:
        from faultflow.runner.progressive_atpg import (
            redundancy_model_id,
            run_progressive_native_atpg,
        )

        total_start = time.perf_counter()
        self.cfg.ensure_workspace()
        cleaned = self._clean_workspace() if clean else 0
        removed = self._purge_transients() if purge else 0
        from faultflow.scan.detection_pipeline import build_scan_pipeline_context

        manifest = self._preflight_sim_scan()
        generic_json = Path(str(manifest["generic_json"]))
        view, pseudo_port_map = build_scan_atpg_view(
            _load_json_object(generic_json), manifest
        )

        # INTEST: fuse the wrapper boundary into the scan-reduced view so the
        # wrapper boundary cells become pseudo-PI/PO alongside the scan FFs.
        wbr_stimulus: dict[str, str] = {}
        wbr_observe: dict[str, str] = {}
        wbr_decoupled: frozenset[int] = frozenset()
        # EXTEST is routed to _sim_extest before reaching here (see sim()); this
        # path handles FUNCTIONAL and INTEST scan campaigns only.
        test_mode = str(self.cfg.test_mode)
        if test_mode == "intest":
            from faultflow.scan.wbr_view import (
                build_wbr_generic_name_map,
                fuse_wbr_into_view,
            )

            generic_data = _load_json_object(generic_json)
            view, wbr_port_map = fuse_wbr_into_view(view, self.cfg.top, test_mode)
            wbr_stimulus, wbr_observe, _decoupled = build_wbr_generic_name_map(
                generic_data,
                self.cfg.top,
                wbr_port_map,
                test_mode,
                manifest,
            )
            wbr_decoupled = frozenset(_decoupled)

        atpg_view_path = self.cfg.intermediate_dir / "scan_atpg_view.json"
        pseudo_map_path = self.cfg.intermediate_dir / "scan_pseudo_port_map.json"
        atpg_view_path.write_text(
            json.dumps(view, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pseudo_map_path.write_text(
            json.dumps(pseudo_port_map, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        netlist = atpg_view_path
        functional_output_order = [
            port
            for port in _port_names(netlist, self.cfg.top, "output", expand_buses=True)
            if not port.startswith("__ppo_")
        ]
        scan_pipeline_ctx = build_scan_pipeline_context(
            self.cfg,
            manifest,
            generic_json,
            pseudo_port_map,
            functional_output_order,
            wbr_stimulus_name_by_port=wbr_stimulus,
            wbr_observe_name_by_port=wbr_observe,
            wbr_decoupled_bits=wbr_decoupled,
        )

        from faultflow.scan.atpg_view import ATPG_VIEW_SCHEMA_VER

        # An INTEST campaign cannot resume against a FUNCTIONAL one: test_mode is
        # part of config_hash (see _config_fingerprint_payload), and the fused
        # atpg_view netlist_hash also differs, so the resume guard already holds.
        fp = self._fingerprint(netlist)
        fp["manifest_hash"] = str(manifest.get("generic_json_hash", ""))
        fp["atpg_view_schema_ver"] = ATPG_VIEW_SCHEMA_VER
        with self._db(scan=True) as conn:
            campaign_id = self._check_fingerprint(conn, fp, scan=True)
            abort_pending_candidates(conn, campaign_id)

        model_id = redundancy_model_id(fp)
        vectors, atpg_stats, run_id, atpg_seconds, fault_sim_seconds = (
            run_progressive_native_atpg(
                self.cfg,
                netlist,
                model_id,
                campaign_id=campaign_id,
                max_rounds=max_rounds,
                target_coverage=target_coverage,
                vector_source="scan_native_sat_atpg",
                campaign_type=CAMPAIGN_TYPE_SCAN,
                scan_ctx=scan_pipeline_ctx,
                scan_pattern_out=export_patterns,
            )
        )
        if self.cfg.atpg.compaction != "none":
            from faultflow.scan.cell_map import resolve_scan_cell_map

            scan_cell_map = str(resolve_scan_cell_map(self.cfg))
            if self.cfg.fault_model.model == "transition":
                # Two-frame scan transition compaction MUST re-grade via the
                # two-capture protocol sim, not the single-frame engine (which
                # over-counts and would lose transition coverage).
                from faultflow.scan.detection_pipeline import (
                    compact_run_scan_transition,
                )

                core = _load_core()
                if core is None:
                    raise RunnerError("C++ extension _faultflow_core is required")
                vectors, run_id, raw_vectors_f = compact_run_scan_transition(
                    core,
                    scan_ctx=scan_pipeline_ctx,
                    reduced_json_path=str(netlist),
                    reduced_cell_map=scan_cell_map,
                    generic_cell_map=scan_cell_map,
                    db_path=str(self.cfg.db_path),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    vectors=vectors,
                    unsupported=self.cfg.simulation.unsupported_cells,
                    launch_mode=self.cfg.fault_model.launch,
                )
                raw_vectors_scan = int(raw_vectors_f)
            else:
                from faultflow.runner.compaction import compact_run

                vectors, run_id, raw_vectors_scan = compact_run(
                    json_path=str(netlist),
                    cell_map_path=scan_cell_map,
                    db_path=str(self.cfg.db_path),
                    campaign_id=campaign_id,
                    run_id=run_id,
                    vectors=vectors,
                    unsupported=self.cfg.simulation.unsupported_cells,
                )
        else:
            raw_vectors_scan = vectors.count
        total_seconds = time.perf_counter() - total_start
        self._write_run_timings(run_id, atpg_seconds, fault_sim_seconds, total_seconds)

        scan_context = {
            "pseudo_port_map": pseudo_port_map,
            "manifest_hash": str(manifest.get("generic_json_hash", "")),
        }
        with self._db(scan=True) as conn:
            json_path, txt_path, report = write_reports(
                conn,
                self.cfg,
                scan_context=scan_context,
                campaign_id=campaign_id,
            )

        purge_note = f" purged_transients={removed}" if purge else ""
        clean_note = f" cleaned_db_files={cleaned}" if clean else ""
        scan_compaction_note = (
            f" raw_vectors={raw_vectors_scan}"
            if raw_vectors_scan != vectors.count
            else ""
        )
        return (
            f"sim complete top={self.cfg.top} mode=scan "
            f"vectors={vectors.count}{scan_compaction_note} "
            f"source={vectors.source} sidecar={atpg_view_path} "
            f"coverage={report['summary']['coverage_percent']:.3f}% "
            f"atpg_seconds={atpg_seconds:.3f} "
            f"fault_sim_seconds={fault_sim_seconds:.3f} "
            f"atpg_terminal={atpg_stats.terminal_reason} "
            f"report={txt_path} json={json_path}{purge_note}{clean_note}"
        )

    def _sim_extest(
        self,
        *,
        purge: bool = False,
        clean: bool = False,
        max_rounds: int | None = None,
        target_coverage: float | None = None,
    ) -> str:
        """EXTEST coverage on a scan-wrapped core.

        EXTEST holds the core dead/safe and tests only the wrapper boundary +
        interconnect, so after fusing the scan-reduced view with
        ``fuse_wbr_into_view(mode="extest")`` the result is a COMBINATIONAL
        netlist: ``__wbo_ctl_*`` inputs + top PIs drive the interconnect,
        ``__wbi_obs_*`` outputs + top POs observe it, and the core is const-0
        safed with its scan observe points dropped. We therefore run plain
        combinational native ATPG on the fused view -- not the scan protocol
        pipeline, which exists only to reconcile abstracted FF Q-stems that this
        view doesn't have. Dead-core faults are unobservable here and fall out as
        SAT-redundant, so coverage reflects the testable interconnect.
        """
        from faultflow.runner.progressive_atpg import (
            redundancy_model_id,
            run_progressive_native_atpg,
        )
        from faultflow.scan.atpg_view import ATPG_VIEW_SCHEMA_VER
        from faultflow.scan.cell_map import resolve_scan_cell_map
        from faultflow.scan.wbr_view import (
            WBR_SCAN_EXTEST_VIEW_SCHEMA_VER,
            fuse_wbr_into_view,
        )

        total_start = time.perf_counter()
        self.cfg.ensure_workspace()
        cleaned = self._clean_workspace() if clean else 0
        removed = self._purge_transients() if purge else 0

        manifest = self._preflight_sim_scan()
        generic_json = Path(str(manifest["generic_json"]))
        view, _pseudo_port_map = build_scan_atpg_view(
            _load_json_object(generic_json), manifest
        )
        view, _wbr_port_map = fuse_wbr_into_view(view, self.cfg.top, "extest")

        atpg_view_path = self.cfg.intermediate_dir / "scan_atpg_view.json"
        atpg_view_path.write_text(
            json.dumps(view, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        netlist = atpg_view_path
        scan_cell_map = resolve_scan_cell_map(self.cfg)

        fp = self._fingerprint(netlist)
        fp["manifest_hash"] = str(manifest.get("generic_json_hash", ""))
        fp["atpg_view_schema_ver"] = (
            f"{ATPG_VIEW_SCHEMA_VER}+{WBR_SCAN_EXTEST_VIEW_SCHEMA_VER}"
        )
        # EXTEST uses a separate campaign_type so it never collides with the
        # INTEST/scan campaign in ensure_campaign's latest-campaign lookup.
        with self._db(scan=True) as conn:
            campaign_id = ensure_campaign(conn, CAMPAIGN_TYPE_SCAN_EXTEST, fp)
            abort_pending_candidates(conn, campaign_id)

        model_id = redundancy_model_id(fp)
        # Fusion removed every $wbc_* cell (replaced by const-0 safe drivers +
        # observe buffers + ctl/obs ports), so cg.wrapper_cells is empty and the
        # C++ mode-aware gate is a no-op: combinational ATPG runs plain on the
        # EXTEST configuration already baked into the netlist structure.
        vectors, atpg_stats, run_id, atpg_seconds, fault_sim_seconds = (
            run_progressive_native_atpg(
                self.cfg,
                netlist,
                model_id,
                campaign_id=campaign_id,
                max_rounds=max_rounds,
                target_coverage=target_coverage,
                cell_map_path=scan_cell_map,
            )
        )

        if self.cfg.atpg.compaction != "none":
            from faultflow.runner.compaction import compact_run

            vectors, run_id, raw_vectors = compact_run(
                json_path=str(netlist),
                cell_map_path=str(scan_cell_map),
                db_path=str(self.cfg.db_path),
                campaign_id=campaign_id,
                run_id=run_id,
                vectors=vectors,
                unsupported=self.cfg.simulation.unsupported_cells,
            )
        else:
            raw_vectors = vectors.count

        total_seconds = time.perf_counter() - total_start
        self._write_run_timings(run_id, atpg_seconds, fault_sim_seconds, total_seconds)

        scan_context = {
            "pseudo_port_map": {},
            "manifest_hash": str(manifest.get("generic_json_hash", "")),
        }
        with self._db(scan=True) as conn:
            json_path, txt_path, report = write_reports(
                conn,
                self.cfg,
                scan_context=scan_context,
                campaign_id=campaign_id,
            )

        purge_note = f" purged_transients={removed}" if purge else ""
        clean_note = f" cleaned_db_files={cleaned}" if clean else ""
        compaction_note = (
            f" raw_vectors={raw_vectors}" if raw_vectors != vectors.count else ""
        )
        return (
            f"sim complete top={self.cfg.top} mode=extest "
            f"vectors={vectors.count}{compaction_note} "
            f"source={vectors.source} sidecar={atpg_view_path} "
            f"coverage={report['summary']['coverage_percent']:.3f}% "
            f"atpg_seconds={atpg_seconds:.3f} "
            f"fault_sim_seconds={fault_sim_seconds:.3f} "
            f"atpg_terminal={atpg_stats.terminal_reason} "
            f"report={txt_path} json={json_path}{purge_note}{clean_note}"
        )

    def status(self, scan: bool = False) -> str:
        with self._db(scan=scan) as conn:
            campaign_id = latest_campaign_id(conn, self._campaign_type(scan))
            if campaign_id is None:
                return f"top={self.cfg.top} scan_mode={str(scan).lower()} coverage=n/a"
            data = summary(conn, campaign_id=campaign_id)
            cov = data["coverage_percent"]
            cov_text = "n/a" if cov is None else f"{cov:.3f}%"
            run = conn.execute(
                """
                SELECT atpg_terminal_reason, atpg_rounds, atpg_sat, atpg_unsat,
                       atpg_timeout, atpg_unknown, atpg_rejected_candidates,
                       atpg_generated_vectors, atpg_accepted_vectors,
                       atpg_generation_seconds, fault_simulation_seconds,
                       total_sim_seconds
                FROM runs
                WHERE campaign_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (campaign_id,),
            ).fetchone()
            atpg_note = ""
            if run is not None:
                atpg_seconds = float(run["atpg_generation_seconds"] or 0.0)
                fault_sim_seconds = float(run["fault_simulation_seconds"] or 0.0)
                total_sim_seconds = float(run["total_sim_seconds"] or 0.0)
                atpg_note = (
                    f" atpg_seconds={atpg_seconds:.3f}"
                    f" fault_sim_seconds={fault_sim_seconds:.3f}"
                    f" total_sim_seconds={total_sim_seconds:.3f}"
                    f" atpg_terminal={run['atpg_terminal_reason'] or 'n/a'}"
                    f" rounds={run['atpg_rounds']}"
                    f" sat={run['atpg_sat']} unsat={run['atpg_unsat']}"
                    f" timeout={run['atpg_timeout']} unknown={run['atpg_unknown']}"
                    f" rejected={run['atpg_rejected_candidates']}"
                    f" generated={run['atpg_generated_vectors']}"
                    f" accepted={run['atpg_accepted_vectors']}"
                )
            return (
                f"top={self.cfg.top} scan_mode={str(scan).lower()} coverage={cov_text} "
                f"detected={data['detected']} denominator={data['denominator']} "
                f"undetected={data['undetected']} redundant={data.get('redundant', 0)} "
                f"protocol_unresolved={data.get('protocol_unresolved', 0)} "
                f"collapsed={data['collapsed']} "
                f"excluded_blackbox={data['excluded_blackbox']} "
                f"excluded_clock={data['excluded_clock']} "
                f"excluded_reset={data['excluded_reset']} "
                f"xdomain={data.get('excluded_cross_domain', 0)}{atpg_note}"
            )
