from __future__ import annotations

import hashlib
import json
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
from faultflow.config import FaultflowConfig
from faultflow.db import connect, init_schema, summary
from faultflow.reporter import write_reports
from faultflow.scan import (
    ScanError,
    plan_scan_json,
    stitch_scan_json,
    write_scan_techmap,
    run_scan_techmap,
)
from faultflow.scan.checks import check_scan_structure
from faultflow.scan.reports import (
    format_dry_run,
    load_manifest,
    manifest_from_result,
    utc_timestamp,
    write_scan_artifacts,
)
from faultflow.verify import IverilogVerifier, VerificationError


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


def _port_names(path: Path, top: str, direction: str) -> list[str]:
    _, module = _json_top_module(path, top)
    ports = module.get("ports")
    if not isinstance(ports, dict):
        raise RunnerError(f"module {top} ports must be an object")
    out: list[str] = []
    for name, port in ports.items():
        if isinstance(port, dict) and port.get("direction") == direction:
            bits = port.get("bits")
            if isinstance(bits, list) and len(bits) == 1 and isinstance(bits[0], int):
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
    candidates = [
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

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self.cfg.db_path)
        init_schema(conn)
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
        finally:
            conn.close()

    def _config_fingerprint_payload(self) -> dict[str, object]:
        return {
            "top": self.cfg.top,
            "cell_lib": str(self.cfg.cell_lib),
            "collapsing": self.cfg.fault_model.collapsing,
            "unsupported_cells": self.cfg.simulation.unsupported_cells,
            "include_clock_faults": self.cfg.fault_model.include_clock_faults,
            "include_reset_faults": self.cfg.fault_model.include_reset_faults,
            "atpg_tool": self.cfg.atpg.tool,
            "atpg_mode": self.cfg.atpg.mode,
        }

    def _rendered_yosys_script(self, source: Path | None = None) -> str:
        src = source or self._existing_verilog_source()
        return YOSYS_TEMPLATE.format(
            verilog=src if src is not None else self.cfg.netlist,
            top=self.cfg.top,
            liberty=self.cfg.liberty or "",
            json=self.cfg.output_dir / f"{self.cfg.top}.json",
            gate_verilog=self.cfg.output_dir / f"{self.cfg.top}_gate.v",
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
        log = self.cfg.output_dir / "yosys.log"
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
        }

    def _stored_fingerprint(self, conn: sqlite3.Connection) -> dict[str, object] | None:
        row = conn.execute("SELECT * FROM design_fingerprint WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def _check_fingerprint(
        self, conn: sqlite3.Connection, current: dict[str, object]
    ) -> None:
        stored = self._stored_fingerprint(conn)
        if stored is None:
            return
        fields = [
            "top",
            "netlist_hash",
            "cell_lib_hash",
            "collapsing",
            "unsupported_cells",
            "include_clock_faults",
            "include_reset_faults",
            "config_hash",
            "template_hash",
            "yosys_version",
            "faultflow_version",
        ]
        for field in fields:
            if str(stored[field]) != str(current[field]):
                raise FingerprintMismatchError(
                    f"Fingerprint mismatch on {field}; run with a clean output DB"
                )

    def _write_fingerprint(
        self, conn: sqlite3.Connection, fp: dict[str, object]
    ) -> None:
        conn.execute("DELETE FROM design_fingerprint")
        conn.execute(
            """
            INSERT INTO design_fingerprint (
              id, top, netlist_hash, cell_lib_hash, config_hash, template_hash,
              yosys_version, faultflow_version, collapsing, unsupported_cells,
              include_clock_faults, include_reset_faults
            ) VALUES (
              1, :top, :netlist_hash, :cell_lib_hash, :config_hash, :template_hash,
              :yosys_version, :faultflow_version, :collapsing, :unsupported_cells,
              :include_clock_faults, :include_reset_faults
            )
            """,
            fp,
        )
        conn.commit()

    def init(self) -> str:
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            existing_netlist = self._existing_json_netlist()
            if existing_netlist is not None:
                fp = self._fingerprint(existing_netlist)
                self._check_fingerprint(conn, fp)
                if self._stored_fingerprint(conn) is None:
                    self._write_fingerprint(conn, fp)
        return f"initialized {self.cfg.output_dir}"

    def _json_netlist_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if _is_json(self.cfg.netlist):
            candidates.append(self.cfg.netlist)
        candidates.extend(
            [
                self.cfg.output_dir / f"{self.cfg.top}.json",
                self.cfg.output_dir / "design.json",
                Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.json",
                Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.json",
            ]
        )
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
        source = self._find_verilog_source()
        json_path = self.cfg.output_dir / f"{self.cfg.top}.json"
        gate_v = self.cfg.output_dir / f"{self.cfg.top}_gate.v"
        script = self.cfg.output_dir / "yosys_synth.tcl"
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
        (self.cfg.output_dir / "yosys.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(f"Yosys failed; see {self.cfg.output_dir / 'yosys.log'}")
        return json_path

    def _scan_dir(self) -> Path:
        return self.cfg.output_dir / "scan"

    def _scan_manifest_path(self) -> Path:
        return self._scan_dir() / "scan_manifest.json"

    def scan(
        self,
        run_techmap: bool | None = None,
        scan_chains: int | None = None,
        max_chain_length: int | None = None,
        scan_in: str | None = None,
        scan_out: str | None = None,
        scan_enable: str | None = None,
        dry_run: bool = False,
    ) -> str:
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        scan_dir = self._scan_dir()
        scan_dir.mkdir(parents=True, exist_ok=True)
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

        generic_json = scan_dir / f"{self.cfg.top}_scan_generic.json"
        techmap_v = scan_dir / "faultflow_scanff_map.v"
        sky130_v = scan_dir / f"{self.cfg.top}_scan_sky130.v"
        try:
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
            write_scan_techmap(techmap_v)
            techmapped: Path | None = None
            if do_techmap:
                techmapped = run_scan_techmap(
                    generic_json=generic_json,
                    techmap_verilog=techmap_v,
                    output_verilog=sky130_v,
                    top=result.top,
                    log_path=scan_dir / "yosys_scan.log",
                    script_path=scan_dir / "yosys_scan.ys",
                )
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc

        manifest = manifest_from_result(result, netlist, techmap_v, techmapped)
        write_scan_artifacts(scan_dir, manifest)
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
        sky130_v = self._scan_dir() / f"{self.cfg.top}_scan_sky130.v"
        write_scan_techmap(techmap_v)
        try:
            techmapped = run_scan_techmap(
                generic_json=generic_json,
                techmap_verilog=techmap_v,
                output_verilog=sky130_v,
                top=str(manifest["top"]),
                log_path=self._scan_dir() / "yosys_scan.log",
                script_path=self._scan_dir() / "yosys_scan.ys",
            )
        except ScanError as exc:
            raise RunnerError(str(exc)) from exc
        manifest["sky130_verilog"] = str(techmapped)
        write_scan_artifacts(self._scan_dir(), manifest)
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
            self.cfg.atpg.output,
            self.cfg.output_dir / f"{self.cfg.top}atpg.test",
            self.cfg.output_dir / "atpg.test",
            (Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}atpg.test"),
            (Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}atpg.test"),
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
            cycle_count = max(10, int(self._current_scan_cell_count()) + 2)
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
            self.cfg.output_dir / f"{self.cfg.top}.bench",
            self.cfg.output_dir / "design.bench",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.bench",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.bench",
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

    def _sequence_outputs(
        self,
        netlist: Path,
        vectors: VectorSet,
        output_order: list[str],
        clock_name: str,
        extra_inputs: dict[str, bool] | None = None,
    ) -> list[dict[str, bool]]:
        core = _load_core()
        if core is None:
            raise RunnerError(
                "C++ extension _faultflow_core is required. "
                "Run: cmake --build build -- -j2"
            )
        extra = extra_inputs or {}
        input_order = sorted(set(vectors.input_order) | set(extra) | {clock_name})
        sequences = []
        for vector in vectors.vectors:
            base = {name: bool(vector.get(name, False)) for name in input_order}
            base.update(extra)
            low = dict(base)
            high = dict(base)
            low[clock_name] = False
            high[clock_name] = True
            sequences.append([low, high])
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

    def _run_scan_normal_mode_check(
        self,
        manifest: dict[str, object],
        vectors_path: Path | None,
    ) -> tuple[list[str], dict[str, object]]:
        source_json = Path(str(manifest["source_json"]))
        generic_json = Path(str(manifest["generic_json"]))
        output_order = _port_names(source_json, str(manifest["top"]), "output")
        if not output_order:
            raise RunnerError("normal-mode scan check requires at least one PO")
        clock_net_raw = manifest["clock_net"]
        if not isinstance(clock_net_raw, int):
            raise RunnerError("scan manifest clock_net must be an integer")
        clock_net = clock_net_raw
        clock_name = _port_name_for_net(
            source_json, str(manifest["top"]), clock_net, "input"
        )
        if clock_name is None:
            raise RunnerError(f"cannot map scan clock net {clock_net} to an input port")
        vectors, vector_source = self._scan_vector_source(source_json, vectors_path)

        original = self._sequence_outputs(
            source_json, vectors, output_order, clock_name
        )
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
        scanned = self._sequence_outputs(
            generic_json,
            scanned_vectors,
            output_order,
            clock_name,
            extra_inputs=scan_extra,
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

    def scan_check(
        self,
        vectors_path: Path | None = None,
        require_techmap: bool = False,
    ) -> str:
        manifest_path = self._scan_manifest_path()
        if not manifest_path.exists():
            raise RunnerError(f"scan manifest not found: {manifest_path}")
        manifest = load_manifest(manifest_path)
        structural = check_scan_structure(manifest, require_techmap=require_techmap)
        errors = list(structural.errors)
        warnings = list(structural.warnings)
        normal_mode: dict[str, object] | None = None
        if not errors:
            try:
                extra_warnings, normal_mode = self._run_scan_normal_mode_check(
                    manifest, vectors_path
                )
                warnings.extend(extra_warnings)
            except Exception as exc:
                errors.append(str(exc))

        latest_check = {
            "timestamp": utc_timestamp(),
            "status": "PASS" if not errors else "FAIL",
            "warnings": warnings,
            "errors": errors,
            "normal_mode": normal_mode,
        }
        manifest["latest_check"] = latest_check
        write_scan_artifacts(self._scan_dir(), manifest)
        if errors:
            raise RunnerError("scan-check failed: " + "; ".join(errors))
        return (
            f"scan-check PASS top={manifest.get('top')} "
            f"vectors={normal_mode.get('vector_count') if normal_mode else 0} "
            f"manifest={manifest_path}"
        )

    def _find_bench_sidecar(self) -> tuple[Path, list[str]]:
        candidates = [
            self.cfg.output_dir / f"{self.cfg.top}.bench",
            self.cfg.output_dir / "design.bench",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.bench",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.bench",
        ]
        for path in candidates:
            if path.exists():
                return path, parse_bench_inputs(path)
        return self._run_nl2bench()

    def _run_nl2bench(self) -> tuple[Path, list[str]]:
        gate_v = self.cfg.output_dir / f"{self.cfg.top}_gate.v"
        if not gate_v.exists():
            self._run_yosys()
        if not gate_v.exists():
            raise RunnerError(f"Cannot generate BENCH; missing {gate_v}")
        if self.cfg.liberty is None or not self.cfg.liberty.exists():
            raise RunnerError("BENCH generation requires an existing liberty file")

        bench = self.cfg.output_dir / f"{self.cfg.top}.bench"
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
        (self.cfg.output_dir / "nl2bench.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(
                f"nl2bench failed; see {self.cfg.output_dir / 'nl2bench.log'}"
            )
        return bench, parse_bench_inputs(bench)

    def _find_order_sidecar(self) -> tuple[Path, list[str]]:
        return self._find_bench_sidecar()

    def _gate_verilog_candidates(self) -> list[Path]:
        return [
            self.cfg.output_dir / f"{self.cfg.top}_gate.v",
            self.cfg.output_dir / f"{self.cfg.top}_synth.v",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}_synth.v",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.nl.v",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.cut.v",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}_synth.v",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.nl.v",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.cut.v",
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
            self.cfg.atpg.output,
            self.cfg.output_dir / f"{self.cfg.top}atpg.test",
            self.cfg.output_dir / "atpg.test",
            (Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}atpg.test"),
            (Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}atpg.test"),
        ]
        for path in candidates:
            if path.exists():
                return path
        return self._run_quaigh()

    def _run_quaigh(self) -> Path:
        sidecar, _ = self._find_bench_sidecar()
        if sidecar.suffix != ".bench":
            raise RunnerError("Quaigh ATPG input must be .bench")
        output = self.cfg.output_dir / f"{self.cfg.top}atpg.test"
        proc = subprocess.run(
            ["quaigh", "atpg", str(sidecar), "-o", str(output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.cfg.output_dir / "quaigh.log").write_text(proc.stdout, encoding="utf-8")
        if proc.returncode != 0:
            raise RunnerError(
                f"Quaigh failed; see {self.cfg.output_dir / 'quaigh.log'}"
            )
        return output

    def _purge_transients(self) -> int:
        removed = 0
        if not self.cfg.output_dir.exists():
            return removed
        for path in self.cfg.output_dir.rglob("*"):
            if path.name not in TRANSIENT_NAMES and path.suffix not in {".pyc", ".pyo"}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        return removed

    def _clean_db(self) -> int:
        removed = 0
        for path in [
            self.cfg.db_path,
            self.cfg.db_path.with_name(self.cfg.db_path.name + "-wal"),
            self.cfg.db_path.with_name(self.cfg.db_path.name + "-shm"),
            self.cfg.db_path.with_name(self.cfg.db_path.name + "-journal"),
        ]:
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    def _simulate_with_core(
        self,
        netlist: Path,
        vectors: VectorSet,
        vector_source: str,
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
                vectors.vectors,
                vectors.input_order,
                vector_source,
                self.cfg.fault_model.include_clock_faults,
                self.cfg.fault_model.include_reset_faults,
                self.cfg.fault_model.collapsing,
                self.cfg.simulation.unsupported_cells,
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

    def _native_vectors(self, netlist: Path) -> VectorSet:
        core = self._core()
        raw_vectors = list(
            core.native_atpg_vectors(
                str(netlist),
                str(self.cfg.cell_lib),
                self.cfg.simulation.unsupported_cells,
                self.cfg.atpg.random_vectors,
                self.cfg.atpg.sat_conflict_limit,
                self.cfg.atpg.max_sat_vectors,
                self.cfg.fault_model.include_clock_faults,
                self.cfg.fault_model.include_reset_faults,
                self.cfg.fault_model.collapsing,
            )
        )
        input_order = _port_names(netlist, self.cfg.top, "input")
        return VectorSet("native_sat_atpg", input_order, raw_vectors)

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
        path = self.cfg.output_dir / "verified_vectors.json"
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
        verify_dir = self.cfg.output_dir / "verify"
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
    ) -> list[dict[str, bool]]:
        if self.cfg.simulation.verify_tool != "iverilog":
            raise RunnerError("Only verify_tool=iverilog is supported")
        if self.cfg.verilog_models is None:
            raise RunnerError("verification requires verilog_models")
        output_order = parse_bench_outputs(sidecar)
        verifier = IverilogVerifier(
            top=self.cfg.top,
            work_dir=self.cfg.output_dir / "verify",
            gate_verilog=self._find_gate_verilog(),
            verilog_models=[self.cfg.verilog_models],
        )
        try:
            result = verifier.run(input_order, output_order, vectors)
        except VerificationError as exc:
            self._mark_verification_failure(str(exc))
            raise RunnerError(f"verification failed: {exc}") from exc

        cpp_outputs = self._fault_free_outputs_with_core(netlist, vectors, output_order)
        if cpp_outputs != result.expected_outputs:
            error = "C++ fault-free outputs did not match Iverilog golden outputs"
            mismatch_path = self.cfg.output_dir / "verify" / "cpp_crosscheck.txt"
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

    def sim(
        self,
        purge: bool = False,
        clean: bool = False,
        verify: bool | None = None,
        ext: Path | None = None,
    ) -> str:
        total_start = time.perf_counter()
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        cleaned = self._clean_db() if clean else 0
        removed = self._purge_transients() if purge else 0
        netlist = self._find_netlist()
        atpg_start = time.perf_counter()
        if ext is not None:
            vectors, sidecar = self._external_vectors(ext, netlist)
            vector_source = f"external:{ext}"
            atpg_seconds = 0.0
        else:
            vectors = self._native_vectors(netlist)
            sidecar = self.cfg.output_dir / "native_sat_atpg"
            vector_source = "native_sat_atpg"
            atpg_seconds = time.perf_counter() - atpg_start
        verify_enabled = self.cfg.simulation.verify if verify is None else verify
        verified_outputs: list[dict[str, bool]] | None = None

        with self._db() as conn:
            fp = self._fingerprint(netlist)
            self._check_fingerprint(conn, fp)
            if self._stored_fingerprint(conn) is None:
                self._write_fingerprint(conn, fp)

        if verify_enabled:
            if ext is None:
                sidecar, _ = self._find_order_sidecar()
            verified_outputs = self._run_verification(
                netlist, sidecar, vectors, vectors.input_order
            )

        sim_start = time.perf_counter()
        sim_result = self._simulate_with_core(netlist, vectors, vector_source)
        fault_sim_seconds = time.perf_counter() - sim_start
        if verified_outputs is not None:
            run_id = sim_result["run_id"]
            if not isinstance(run_id, (int, str)):
                raise RunnerError(
                    "C++ simulation result did not include a valid run_id"
                )
            self._write_verified_vectors(int(run_id), vectors, verified_outputs)
        run_id = sim_result["run_id"]
        if not isinstance(run_id, (int, str)):
            raise RunnerError("C++ simulation result did not include a valid run_id")
        total_seconds = time.perf_counter() - total_start
        self._write_run_timings(
            int(run_id), atpg_seconds, fault_sim_seconds, total_seconds
        )
        mode = "c++"

        with self._db() as conn:
            json_path, txt_path, report = write_reports(
                conn, self.cfg.output_dir, self.cfg.top
            )

        purge_note = f" purged_transients={removed}" if purge else ""
        clean_note = f" cleaned_db_files={cleaned}" if clean else ""
        return (
            f"sim complete top={self.cfg.top} mode={mode} vectors={vectors.count} "
            f"source={vector_source} sidecar={sidecar} "
            f"coverage={report['summary']['coverage_percent']:.3f}% "
            f"atpg_seconds={atpg_seconds:.3f} "
            f"fault_sim_seconds={fault_sim_seconds:.3f} "
            f"report={txt_path} json={json_path}{purge_note}{clean_note}"
        )

    def status(self) -> str:
        with self._db() as conn:
            data = summary(conn)
            cov = data["coverage_percent"]
            cov_text = "n/a" if cov is None else f"{cov:.3f}%"
            return (
                f"top={self.cfg.top} coverage={cov_text} "
                f"detected={data['detected']} denominator={data['denominator']} "
                f"undetected={data['undetected']} collapsed={data['collapsed']} "
                f"excluded_blackbox={data['excluded_blackbox']} "
                f"excluded_clock={data['excluded_clock']} "
                f"excluded_reset={data['excluded_reset']}"
            )
