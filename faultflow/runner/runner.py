from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
write_blif {blif}
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


def _truthy_attr(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return "1" in value and value.strip("0") != ""
    return False


def _parse_bit(bit: Any) -> int | bool:
    if isinstance(bit, int):
        return bit
    if bit == "0":
        return False
    if bit == "1":
        return True
    raise RunnerError(f"X/Z constants are not supported in Phase 1: {bit}")


def _is_clock(name: str) -> bool:
    low = name.lower()
    return low in {"clk", "clock"} or low.endswith("_clk") or low.endswith("_clock")


def _is_reset(name: str) -> bool:
    low = name.lower()
    return low in {"rst", "reset", "rst_n", "reset_n"} or low.endswith("_rst")


@dataclass
class _Fault:
    net: int
    net_name: str
    compiled_index: int
    fault_type: str
    exclusion: str = "none"
    status: str = "undetected"
    detected_by_vector: int | None = None


class _PythonCombSimulator:
    def __init__(self, json_path: Path, cell_map_path: Path, unsupported_policy: str):
        self.json_path = json_path
        self.cell_map_path = cell_map_path
        self.unsupported_policy = unsupported_policy
        self.cell_map = json.loads(cell_map_path.read_text(encoding="utf-8"))
        design = json.loads(json_path.read_text(encoding="utf-8"))
        self.module = self._top_module(design)
        self.net_names = self._net_names()
        self.input_bits = self._port_bits("input")
        self.output_bits = self._port_bits("output")
        self.blackboxed: set[int] = set()
        self.nodes = self._nodes()
        self.all_nets = self._all_nets()

    def _top_module(self, design: dict[str, Any]) -> dict[str, Any]:
        found: tuple[str, dict[str, Any]] | None = None
        for name, mod in design.get("modules", {}).items():
            attrs = mod.get("attributes", {})
            if _truthy_attr(attrs.get("top", False)):
                found = (name, mod)
                break
        if found is None:
            raise RunnerError(f"No top module in {self.json_path}")
        return found[1]

    def _port_bits(self, direction: str) -> dict[str, int]:
        ports: dict[str, int] = {}
        for name, port in self.module.get("ports", {}).items():
            if port.get("direction") != direction:
                continue
            bits = [_parse_bit(bit) for bit in port.get("bits", [])]
            if len(bits) != 1 or not isinstance(bits[0], int):
                raise RunnerError(
                    f"Only scalar real {direction} ports are supported: {name}"
                )
            ports[name] = bits[0]
        return ports

    def _net_names(self) -> dict[int, str]:
        names: dict[int, str] = {}
        for name, net in self.module.get("netnames", {}).items():
            for bit in net.get("bits", []):
                parsed = _parse_bit(bit)
                if isinstance(parsed, int):
                    names.setdefault(parsed, name)
        return names

    def _lookup_cell(self, raw_type: str) -> dict[str, Any] | None:
        for pattern, entry in self.cell_map.items():
            if fnmatch.fnmatchcase(raw_type, pattern):
                return entry
        return None

    def _conn_bit(self, conns: dict[str, Any], pin: str) -> int | bool:
        bits = conns.get(pin)
        if not bits or len(bits) != 1:
            raise RunnerError(f"Cell pin {pin} must be scalar")
        return _parse_bit(bits[0])

    def _nodes(
        self,
    ) -> list[tuple[dict[str, Any], dict[str, int | bool], dict[str, int]]]:
        nodes = []
        for cell_name, cell in self.module.get("cells", {}).items():
            entry = self._lookup_cell(cell.get("type", ""))
            if entry is None or entry.get("unsupported", False):
                if self.unsupported_policy == "blackbox":
                    for bits in cell.get("connections", {}).values():
                        for bit in bits:
                            parsed = _parse_bit(bit)
                            if isinstance(parsed, int):
                                self.blackboxed.add(parsed)
                    continue
                raise RunnerError(f"Unsupported cell {cell.get('type')} at {cell_name}")
            node_type = entry.get("node_type", "GATE")
            if node_type in {"FF", "LATCH", "TBUF", "ICG"}:
                raise RunnerError(
                    f"Deferred Phase 1 cell {cell.get('type')} at {cell_name}"
                )
            inputs = {
                pin: self._conn_bit(cell.get("connections", {}), pin)
                for pin in entry.get("inputs", [])
            }
            outputs = {
                logical: self._conn_bit(cell.get("connections", {}), raw_pin)
                for logical, raw_pin in entry.get("outputs", {}).items()
            }
            if not all(isinstance(v, int) for v in outputs.values()):
                raise RunnerError(f"Cell {cell_name} drives a constant output")
            nodes.append((entry, inputs, outputs))  # type: ignore[arg-type]
        return nodes

    def _all_nets(self) -> list[int]:
        nets: set[int] = set()
        for bit in self.input_bits.values():
            nets.add(bit)
        for bit in self.output_bits.values():
            nets.add(bit)
        for name, net in self.module.get("netnames", {}).items():
            for bit in net.get("bits", []):
                parsed = _parse_bit(bit)
                if isinstance(parsed, int):
                    nets.add(parsed)
                    self.net_names.setdefault(parsed, name)
        return sorted(nets)

    def _value(self, values: dict[int, bool], bit: int | bool) -> bool:
        if isinstance(bit, bool):
            return bit
        return values.get(bit, False)

    def _eval_node(
        self,
        entry: dict[str, Any],
        inputs: dict[str, int | bool],
        outputs: dict[str, int],
        values: dict[int, bool],
    ) -> None:
        vals = [self._value(values, inputs[pin]) for pin in entry.get("inputs", [])]
        gate = entry.get("gate_type")
        if gate == "INV":
            result = {"Y": not vals[0]}
        elif gate == "BUF":
            result = {"Y": vals[0]}
        elif gate == "AND2":
            result = {"Y": vals[0] and vals[1]}
        elif gate == "OR2":
            result = {"Y": vals[0] or vals[1]}
        elif gate == "NAND2":
            result = {"Y": not (vals[0] and vals[1])}
        elif gate == "NAND3":
            result = {"Y": not (vals[0] and vals[1] and vals[2])}
        elif gate == "NOR2":
            result = {"Y": not (vals[0] or vals[1])}
        elif gate == "NOR3":
            result = {"Y": not (vals[0] or vals[1] or vals[2])}
        elif gate == "XOR2":
            result = {"Y": vals[0] ^ vals[1]}
        elif gate == "XNOR2":
            result = {"Y": not (vals[0] ^ vals[1])}
        elif gate == "AOI21":
            result = {"Y": not ((vals[0] and vals[1]) or vals[2])}
        elif gate == "AOI22":
            result = {"Y": not ((vals[0] and vals[1]) or (vals[2] and vals[3]))}
        elif gate == "OAI21":
            result = {"Y": not ((vals[0] or vals[1]) and vals[2])}
        elif gate == "OAI22":
            result = {"Y": not ((vals[0] or vals[1]) and (vals[2] or vals[3]))}
        elif gate == "MUX2":
            result = {"Y": not ((vals[2] and vals[0]) or ((not vals[2]) and vals[1]))}
        elif gate == "ADDF":
            result = {
                "S": vals[0] ^ vals[1] ^ vals[2],
                "CO": (vals[0] and vals[1])
                or (vals[1] and vals[2])
                or (vals[0] and vals[2]),
            }
        elif gate == "ADDH":
            result = {"S": vals[0] ^ vals[1], "CO": vals[0] and vals[1]}
        else:
            raise RunnerError(f"Unsupported gate_type in fallback simulator: {gate}")
        for logical, value in result.items():
            values[outputs[logical]] = value

    def simulate_fault_free(self, vector: dict[str, bool]) -> dict[int, bool]:
        values = {bit: vector.get(name, False) for name, bit in self.input_bits.items()}
        for entry, inputs, outputs in self.nodes:
            self._eval_node(entry, inputs, outputs, values)
        return values

    def simulate_fault(self, vector: dict[str, bool], fault: _Fault) -> dict[int, bool]:
        values = {bit: vector.get(name, False) for name, bit in self.input_bits.items()}
        if fault.net in values:
            values[fault.net] = fault.fault_type == "sa1"
        for entry, inputs, outputs in self.nodes:
            self._eval_node(entry, inputs, outputs, values)
            if fault.net in outputs.values():
                values[fault.net] = fault.fault_type == "sa1"
        return values

    def enumerate_faults(
        self, include_clock: bool, include_reset: bool
    ) -> list[_Fault]:
        faults: list[_Fault] = []
        for idx, net in enumerate(self.all_nets):
            name = self.net_names.get(net, str(net))
            exclusion = "none"
            if net in self.blackboxed:
                exclusion = "blackbox"
            elif _is_clock(name) and not include_clock:
                exclusion = "clock"
            elif _is_reset(name) and not include_reset:
                exclusion = "reset"
            faults.append(_Fault(net, name, idx, "sa0", exclusion))
            faults.append(_Fault(net, name, idx, "sa1", exclusion))
        return faults

    def run(self, vectors: VectorSet) -> list[_Fault]:
        faults = self.enumerate_faults(False, False)
        for fault in faults:
            if fault.exclusion != "none":
                fault.status = "excluded"
                continue
            for vector_index, vector in enumerate(vectors.vectors, 1):
                good = self.simulate_fault_free(vector)
                bad = self.simulate_fault(vector, fault)
                if any(
                    good.get(bit, False) != bad.get(bit, False)
                    for bit in self.output_bits.values()
                ):
                    fault.status = "detected"
                    fault.detected_by_vector = vector_index
                    break
        return faults


class Runner:
    def __init__(self, cfg: FaultflowConfig):
        self.cfg = cfg

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self.cfg.db_path)
        init_schema(conn)
        return conn

    def _fingerprint(self, netlist: Path | None = None) -> dict[str, object]:
        netlist_path = netlist if netlist is not None else self.cfg.netlist
        return {
            "top": self.cfg.top,
            "netlist_hash": _hash_file(netlist_path),
            "cell_lib_hash": _hash_file(self.cfg.cell_lib),
            "config_hash": _hash_file(self.cfg.path),
            "template_hash": _hash_file(Path("faultflow/templates/yosys_synth.tcl.j2")),
            "yosys_version": "",
            "faultflow_version": "phase1",
            "collapsing": int(self.cfg.fault_model.collapsing),
            "unsupported_cells": self.cfg.simulation.unsupported_cells,
            "include_clock_faults": int(self.cfg.fault_model.include_clock_faults),
            "include_reset_faults": int(self.cfg.fault_model.include_reset_faults),
        }

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
        with self._conn() as conn:
            self._write_fingerprint(conn, self._fingerprint())
        return f"initialized {self.cfg.output_dir}"

    def _find_netlist(self) -> Path:
        candidates = [
            self.cfg.netlist,
            self.cfg.output_dir / f"{self.cfg.top}.json",
            self.cfg.output_dir / "design.json",
            Path("tests/benchmarks/iscas85/synth") / f"{self.cfg.top}.json",
            Path("tests/benchmarks/iscas89/synth") / f"{self.cfg.top}.json",
        ]
        for path in candidates:
            if path.exists():
                return path
        return self._run_yosys()

    def _find_verilog_source(self) -> Path:
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
        raise RunnerError(
            f"Cannot find JSON netlist or Verilog source for top {self.cfg.top}"
        )

    def _run_yosys(self) -> Path:
        if self.cfg.liberty is None or not self.cfg.liberty.exists():
            raise RunnerError("Yosys generation requires an existing liberty file")
        source = self._find_verilog_source()
        json_path = self.cfg.output_dir / f"{self.cfg.top}.json"
        blif_path = self.cfg.output_dir / f"{self.cfg.top}.blif"
        gate_v = self.cfg.output_dir / f"{self.cfg.top}_gate.v"
        script = self.cfg.output_dir / "yosys_synth.tcl"
        script.write_text(
            YOSYS_TEMPLATE.format(
                verilog=source,
                top=self.cfg.top,
                liberty=self.cfg.liberty,
                json=json_path,
                blif=blif_path,
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
        cmd = [str(nl2bench if nl2bench.exists() else "nl2bench")]
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

    def _write_vectors(
        self, conn: sqlite3.Connection, run_id: int, vectors: VectorSet
    ) -> None:
        for idx, vector in enumerate(vectors.vectors, 1):
            pattern = "".join(
                "1" if vector[name] else "0" for name in vectors.input_order
            )
            conn.execute(
                """
                INSERT INTO vectors(run_id, source, vector_index, pattern)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, vectors.source, idx, pattern),
            )

    def _write_faults(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        faults: list[_Fault],
    ) -> None:
        conn.execute("DELETE FROM fault_detections")
        conn.execute("DELETE FROM faults")
        for fault in faults:
            cur = conn.execute(
                """
                INSERT INTO faults(
                  net_id, net_name, compiled_net_index, fault_type, status,
                  exclusion, detected_by_vector
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fault.net,
                    fault.net_name,
                    fault.compiled_index,
                    fault.fault_type,
                    fault.status,
                    fault.exclusion,
                    fault.detected_by_vector,
                ),
            )
            if fault.detected_by_vector is not None:
                conn.execute(
                    """
                    INSERT INTO fault_detections(
                      fault_id, run_id, vector_index, obs_net
                    )
                    VALUES (?, ?, ?, NULL)
                    """,
                    (cur.lastrowid, run_id, fault.detected_by_vector),
                )

    def _simulate_with_core(
        self,
        netlist: Path,
        vectors: VectorSet,
        vector_path: Path,
    ) -> dict[str, object] | None:
        core = _load_core()
        if core is None:
            return None
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

        with self._conn() as conn:
            self._write_fingerprint(conn, self._fingerprint(netlist))

        core_summary = self._simulate_with_core(netlist, vectors, vector_path)
        mode = "c++"
        if core_summary is None:
            mode = "python-fallback"
            simulator = _PythonCombSimulator(
                netlist, self.cfg.cell_lib, self.cfg.simulation.unsupported_cells
            )
            faults = simulator.run(vectors)
            with self._conn() as conn:
                conn.execute("DELETE FROM vectors")
                run = conn.execute(
                    """
                    INSERT INTO runs(status, vector_source, vector_count)
                    VALUES ('running', ?, ?)
                    """,
                    (str(vector_path), vectors.count),
                )
                if run.lastrowid is None:
                    raise RunnerError("SQLite did not return a run id")
                run_id = int(run.lastrowid)
                self._write_vectors(conn, run_id, vectors)
                self._write_faults(conn, run_id, faults)
                data = summary(conn)
                conn.execute(
                    """
                    UPDATE runs
                    SET status='complete', completed_at=CURRENT_TIMESTAMP, coverage=?
                    WHERE id=?
                    """,
                    (data["coverage_percent"], run_id),
                )

        with self._conn() as conn:
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
        with self._conn() as conn:
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
