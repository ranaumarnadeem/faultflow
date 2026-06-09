from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PatternError(RuntimeError):
    pass


@dataclass(frozen=True)
class VectorSet:
    source: str
    input_order: list[str]
    vectors: list[dict[str, bool]]

    @property
    def count(self) -> int:
        return len(self.vectors)


@dataclass(frozen=True)
class BenchPorts:
    inputs: list[str]
    outputs: list[str]


def _parse_bench_port_line(raw: str, path: Path) -> tuple[str, str] | None:
    line = raw.split("#", 1)[0].strip()
    upper = line.upper()
    port_kind: str | None = None
    if upper.startswith("INPUT(") or upper.startswith("PINPUT("):
        port_kind = "input"
    elif upper.startswith("OUTPUT(") or upper.startswith("POUTPUT("):
        port_kind = "output"
    if port_kind is None:
        return None
    start = line.find("(")
    end = line.rfind(")")
    if start < 0 or end <= start + 1:
        raise PatternError(f"Malformed BENCH {port_kind} line in {path}: {raw}")
    return port_kind, line[start + 1 : end].strip()


def parse_bench_io(path: str | Path) -> BenchPorts:
    p = Path(path)
    inputs: list[str] = []
    outputs: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        parsed = _parse_bench_port_line(raw, p)
        if parsed is None:
            continue
        kind, name = parsed
        if kind == "input":
            inputs.append(name)
        else:
            outputs.append(name)
    if not inputs:
        raise PatternError(f"BENCH inputs not found: {p}")
    if not outputs:
        raise PatternError(f"BENCH outputs not found: {p}")
    return BenchPorts(inputs=inputs, outputs=outputs)


def parse_bench_inputs(path: str | Path) -> list[str]:
    return parse_bench_io(path).inputs


def parse_bench_outputs(path: str | Path) -> list[str]:
    return parse_bench_io(path).outputs


def _parse_line(line: str, line_no: int, expected_index: int) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("*"):
        return None

    pieces = stripped.split(":")
    if len(pieces) > 2:
        raise PatternError(f"Line {line_no}: expected optional INDEX: PATTERN")
    if len(pieces) == 2:
        try:
            index = int(pieces[0].strip())
        except ValueError as exc:
            raise PatternError(f"Line {line_no}: invalid pattern index") from exc
        if index != expected_index:
            raise PatternError(
                f"Line {line_no}: index {index} does not match "
                f"expected {expected_index}"
            )
        pattern_text = pieces[1].strip()
    else:
        pattern_text = pieces[0].strip()

    tokens = pattern_text.split()
    if len(tokens) != 1:
        raise PatternError(f"Line {line_no}: combinational .test requires one timestep")
    pattern = tokens[0]
    if not pattern:
        raise PatternError(f"Line {line_no}: empty pattern")
    invalid = sorted({ch for ch in pattern if ch not in {"0", "1"}})
    if invalid:
        raise PatternError(f"Line {line_no}: invalid pattern chars {''.join(invalid)}")
    return pattern


def parse_quaigh_test(
    path: str | Path, input_order: list[str], source: str | None = None
) -> VectorSet:
    p = Path(path)
    if not input_order:
        raise PatternError("missing input order for .test parsing")

    vectors: list[dict[str, bool]] = []
    expected_index = 1
    for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        pattern = _parse_line(line, line_no, expected_index)
        if pattern is None:
            continue
        if len(pattern) != len(input_order):
            raise PatternError(
                f"Line {line_no}: width {len(pattern)} does not match "
                f"{len(input_order)} inputs"
            )
        vectors.append(
            {name: value == "1" for name, value in zip(input_order, pattern)}
        )
        expected_index += 1

    if not vectors:
        raise PatternError(f"No vectors found in {p}")
    return VectorSet(source=source or str(p), input_order=input_order, vectors=vectors)
