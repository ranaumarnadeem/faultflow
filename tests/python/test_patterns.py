from pathlib import Path

import pytest

from faultflow.atpg import PatternError, parse_bench_inputs, parse_quaigh_test


def test_parse_quaigh_test_strict_comb(tmp_path: Path) -> None:
    test_file = tmp_path / "demo.test"
    test_file.write_text("* comment\n1: 01\n2: 10\n", encoding="utf-8")

    vectors = parse_quaigh_test(test_file, ["a", "b"])

    assert vectors.count == 2
    assert vectors.vectors == [{"a": False, "b": True}, {"a": True, "b": False}]


@pytest.mark.parametrize(
    "text",
    [
        "1: 01 10\n",
        "1: 0x\n",
        "2: 01\n",
        "1:\n",
    ],
)
def test_parse_quaigh_test_rejects_non_combinational_subset(
    tmp_path: Path, text: str
) -> None:
    test_file = tmp_path / "bad.test"
    test_file.write_text(text, encoding="utf-8")

    with pytest.raises(PatternError):
        parse_quaigh_test(test_file, ["a", "b"])


def test_parse_existing_benchmark_vectors() -> None:
    bench = Path("tests/benchmarks/iscas85/synth/c17.bench")
    test_file = Path("tests/benchmarks/iscas85/synth/c17atpg.test")

    order = parse_bench_inputs(bench)
    vectors = parse_quaigh_test(test_file, order)

    assert order == ["N1", "N2", "N3", "N6", "N7"]
    assert vectors.count == 4
