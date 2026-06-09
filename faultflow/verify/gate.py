from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from faultflow.atpg import VectorSet


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CycleSpec:
    cycle: int
    clock_edge: str
    reset_state: int
    sample_outputs: bool
    settle_cycles: int = 0

    @classmethod
    def combinational(cls) -> "CycleSpec":
        return cls(
            cycle=0,
            clock_edge="NONE",
            reset_state=0,
            sample_outputs=True,
            settle_cycles=0,
        )


@dataclass(frozen=True)
class VectorContract:
    initial_cycles: int
    reset_active_until: int
    sample_on_cycle: int
    x_settle_allowed: bool

    @classmethod
    def combinational(cls) -> "VectorContract":
        return cls(
            initial_cycles=0,
            reset_active_until=-1,
            sample_on_cycle=0,
            x_settle_allowed=False,
        )

    def is_sample_cycle(self, cycle: CycleSpec | int) -> bool:
        cycle_no = cycle.cycle if isinstance(cycle, CycleSpec) else cycle
        return cycle_no == self.sample_on_cycle

    def is_settle_cycle(self, cycle: CycleSpec | int) -> bool:
        cycle_no = cycle.cycle if isinstance(cycle, CycleSpec) else cycle
        return cycle_no < self.sample_on_cycle


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    expected_outputs: list[dict[str, bool]]
    report_json: Path
    report_txt: Path


_SAMPLE_RE = re.compile(r"^FFVERIFY_VECTOR\s+(\d+)\s+([01xXzZ]+)\s*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _verilog_ident(name: str) -> str:
    if _IDENT_RE.match(name):
        return name
    if any(ch.isspace() for ch in name):
        raise VerificationError(
            f"Unsupported Verilog identifier with whitespace: {name}"
        )
    return f"\\{name} "


def _validate_vectors(pis: list[str], vectors: VectorSet) -> None:
    for index, vector in enumerate(vectors.vectors, 1):
        for pi in pis:
            if pi not in vector:
                raise VerificationError(f"vector {index}: missing PI {pi}")


def render_testbench(
    top: str,
    input_order: list[str],
    output_order: list[str],
    vectors: VectorSet,
) -> str:
    if not input_order:
        raise VerificationError("verification requires at least one PI")
    if not output_order:
        raise VerificationError("verification requires at least one PO")
    _validate_vectors(input_order, vectors)

    lines = [
        "`timescale 1ns/1ps",
        "module ff_verify_tb;",
    ]
    for name in input_order:
        lines.append(f"reg {_verilog_ident(name)};")
    for name in output_order:
        lines.append(f"wire {_verilog_ident(name)};")

    ports = [
        f".{_verilog_ident(name)}({_verilog_ident(name)})"
        for name in [*input_order, *output_order]
    ]
    lines.append(f"{_verilog_ident(top)} dut (")
    for idx, port in enumerate(ports):
        suffix = "," if idx + 1 < len(ports) else ""
        lines.append(f"  {port}{suffix}")
    lines.append(");")
    lines.append("initial begin")

    output_concat = ", ".join(_verilog_ident(name) for name in output_order)
    for index, vector in enumerate(vectors.vectors, 1):
        for pi in input_order:
            value = "1" if vector[pi] else "0"
            lines.append(f"  {_verilog_ident(pi)} = 1'b{value};")
        lines.append("  #10;")
        lines.append(
            f'  $display("FFVERIFY_VECTOR %0d %b", {index}, ' f"{{{output_concat}}});"
        )
    lines.append("  $finish;")
    lines.append("end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def parse_iverilog_samples(
    output: str, output_order: list[str]
) -> list[dict[str, bool]]:
    samples: list[dict[str, bool]] = []
    for raw in output.splitlines():
        match = _SAMPLE_RE.match(raw.strip())
        if match is None:
            continue
        vector_index = int(match.group(1))
        bits = match.group(2)
        if len(bits) != len(output_order):
            raise VerificationError(
                f"vector {vector_index}: output width {len(bits)} does not match "
                f"{len(output_order)} POs"
            )
        sample: dict[str, bool] = {}
        for name, bit in zip(output_order, bits):
            if bit in {"x", "X", "z", "Z"}:
                raise VerificationError(
                    f"vector {vector_index}: output {name} is {bit.lower()}"
                )
            sample[name] = bit == "1"
        samples.append(sample)
    if not samples:
        raise VerificationError("Iverilog produced no FFVERIFY_VECTOR samples")
    return samples


class IverilogVerifier:
    def __init__(
        self,
        top: str,
        work_dir: Path,
        gate_verilog: Path,
        verilog_models: list[Path],
    ):
        self.top = top
        self.work_dir = work_dir
        self.gate_verilog = gate_verilog
        self.verilog_models = verilog_models

    def _write_reports(
        self, passed: bool, expected: list[dict[str, bool]], errors: list[str]
    ) -> VerificationResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.work_dir / "verification_report.json"
        txt_path = self.work_dir / "verification_report.txt"
        payload = {
            "version": 1,
            "metadata": {"top": self.top, "tool": "iverilog"},
            "passed": passed,
            "vector_count": len(expected),
            "expected_outputs": expected,
            "errors": errors,
        }
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        lines = [
            f"verification top={self.top} tool=iverilog",
            f"passed={str(passed).lower()} vectors={len(expected)}",
        ]
        lines.extend(f"error: {error}" for error in errors)
        txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return VerificationResult(passed, expected, json_path, txt_path)

    def run(
        self,
        input_order: list[str],
        output_order: list[str],
        vectors: VectorSet,
    ) -> VerificationResult:
        iverilog = shutil.which("iverilog")
        if iverilog is None:
            raise VerificationError("verification requires iverilog on PATH")
        vvp_tool = shutil.which("vvp")
        if vvp_tool is None:
            raise VerificationError("verification requires vvp on PATH")
        if not self.gate_verilog.exists():
            raise VerificationError(f"missing gate Verilog: {self.gate_verilog}")
        if not self.verilog_models:
            raise VerificationError("verification requires verilog_models")
        for model in self.verilog_models:
            if not model.exists():
                raise VerificationError(f"missing verilog_models file: {model}")

        self.work_dir.mkdir(parents=True, exist_ok=True)
        tb = self.work_dir / "testbench.v"
        exe = self.work_dir / "verify.vvp"
        tb.write_text(
            render_testbench(self.top, input_order, output_order, vectors),
            encoding="utf-8",
        )

        compile_cmd = [
            iverilog,
            "-g2012",
            "-o",
            str(exe),
            str(tb),
            str(self.gate_verilog),
            *[str(model) for model in self.verilog_models],
        ]
        compile_proc = subprocess.run(
            compile_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.work_dir / "iverilog.log").write_text(
            compile_proc.stdout, encoding="utf-8"
        )
        if compile_proc.returncode != 0:
            error = f"iverilog failed; see {self.work_dir / 'iverilog.log'}"
            self._write_reports(False, [], [error])
            raise VerificationError(error)

        run_proc = subprocess.run(
            [vvp_tool, str(exe)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (self.work_dir / "vvp.log").write_text(run_proc.stdout, encoding="utf-8")
        if run_proc.returncode != 0:
            error = f"vvp failed; see {self.work_dir / 'vvp.log'}"
            self._write_reports(False, [], [error])
            raise VerificationError(error)

        try:
            expected = parse_iverilog_samples(run_proc.stdout, output_order)
        except VerificationError as exc:
            self._write_reports(False, [], [str(exc)])
            raise

        if len(expected) != vectors.count:
            error = (
                f"Iverilog produced {len(expected)} samples for "
                f"{vectors.count} vectors"
            )
            self._write_reports(False, expected, [error])
            raise VerificationError(error)

        return self._write_reports(True, expected, [])
