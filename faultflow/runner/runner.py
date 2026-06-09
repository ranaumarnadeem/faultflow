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
    parse_bench_outputs,
    parse_quaigh_test,
)
from faultflow.config import FaultflowConfig
from faultflow.db import connect, init_schema, summary
from faultflow.reporter import write_reports
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

    def sim(self, purge: bool = False, verify: bool | None = None) -> str:
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        removed = self._purge_transients() if purge else 0
        netlist = self._find_netlist()
        sidecar, input_order = self._find_order_sidecar()
        vector_path = self._find_vectors()
        vectors = parse_quaigh_test(vector_path, input_order, source=str(vector_path))
        verify_enabled = self.cfg.simulation.verify if verify is None else verify
        verified_outputs: list[dict[str, bool]] | None = None

        with self._db() as conn:
            fp = self._fingerprint(netlist)
            self._check_fingerprint(conn, fp)
            if self._stored_fingerprint(conn) is None:
                self._write_fingerprint(conn, fp)

        if verify_enabled:
            verified_outputs = self._run_verification(
                netlist, sidecar, vectors, input_order
            )

        sim_result = self._simulate_with_core(netlist, vectors, vector_path)
        if verified_outputs is not None:
            run_id = sim_result["run_id"]
            if not isinstance(run_id, (int, str)):
                raise RunnerError(
                    "C++ simulation result did not include a valid run_id"
                )
            self._write_verified_vectors(int(run_id), vectors, verified_outputs)
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
