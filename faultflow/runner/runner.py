from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from faultflow.atpg import (
    VectorSet,
    parse_bench_inputs,
    parse_quaigh_test,
)
from faultflow.config import FaultflowConfig
from faultflow.db import connect, init_schema, summary
from faultflow.reporter import write_reports


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
        if self.cfg.netlist.suffix in {".v", ".sv"}:
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
                match = re.search(r"Yosys\s+(.+)$", line)
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
            "faultflow_version": "phase1",
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
        candidates = self._json_netlist_candidates()
        for path in candidates:
            if path.exists():
                return path
        return self._run_yosys()

    def _find_verilog_source(self) -> Path:
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

    def _simulate_with_core(
        self,
        netlist: Path,
        vectors: VectorSet,
        vector_path: Path,
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
                str(vector_path),
                self.cfg.fault_model.include_clock_faults,
                self.cfg.fault_model.include_reset_faults,
                self.cfg.fault_model.collapsing,
                self.cfg.simulation.unsupported_cells,
            )
        )

    def sim(self, purge: bool = False) -> str:
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        removed = self._purge_transients() if purge else 0
        netlist = self._find_netlist()
        sidecar, input_order = self._find_order_sidecar()
        vector_path = self._find_vectors()
        vectors = parse_quaigh_test(vector_path, input_order, source=str(vector_path))

        with self._db() as conn:
            fp = self._fingerprint(netlist)
            self._check_fingerprint(conn, fp)
            if self._stored_fingerprint(conn) is None:
                self._write_fingerprint(conn, fp)

        self._simulate_with_core(netlist, vectors, vector_path)
        mode = "c++"

        with self._db() as conn:
            json_path, txt_path, report = write_reports(
                conn, self.cfg.output_dir, self.cfg.top
            )

        purge_note = f" purged_transients={removed}" if purge else ""
        return (
            f"sim complete top={self.cfg.top} mode={mode} vectors={vectors.count} "
            f"sidecar={sidecar} "
            f"coverage={report['summary']['coverage_percent']:.3f}% "
            f"report={txt_path} json={json_path}{purge_note}"
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
