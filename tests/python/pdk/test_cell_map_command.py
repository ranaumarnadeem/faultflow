"""`ff.py cell-map`: a cell map for any library, from its Liberty, with FaultFlow's own
internal cells (the scan flop it stitches, its observe buffers) carried over."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main

LIB = """library ("demo") {
cell ("demo__nand2_1") {
 pin ("A") { direction : "input"; }
 pin ("B") { direction : "input"; }
 pin ("Y") { direction : "output"; function : "!(A&B)"; }
}
cell ("demo__dlxtp_1") {
 latch ("IQ","IQ_N") { data_in : "D"; enable : "GATE"; }
 pin ("D") { direction : "input"; }
 pin ("GATE") { direction : "input"; }
 pin ("Q") { direction : "output"; function : "IQ"; }
}
}
"""


@pytest.mark.unit
def test_cell_map_writes_the_map_and_its_report(
    tmp_path: Path, require_cpp_core: None, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_path / "demo.lib"
    lib.write_text(LIB, encoding="utf-8")
    out = tmp_path / "demo.json"
    report = tmp_path / "demo_report.json"
    rc = main(
        ["cell-map", "--liberty", str(lib), "-o", str(out), "--report", str(report)]
    )
    assert rc == 0
    cm = json.loads(out.read_text(encoding="utf-8"))
    assert cm["demo__nand2_*"]["gate_type"] == "NAND2"
    assert cm["demo__dlxtp_*"]["unsupported"] is True
    # FaultFlow's own cells, from the default internal-cell source.
    assert "$scanff_faultflow" in cm
    listed = json.loads(report.read_text(encoding="utf-8"))
    assert listed == [
        {
            "cell": "demo__dlxtp_1",
            "kind": "latch",
            "detail": "latch (out of scope: no X-state)",
        }
    ]
    printed = capsys.readouterr().out
    assert "2 cells" in printed and "1 modelled" in printed and "1 listed" in printed


@pytest.mark.unit
def test_cell_map_refuses_a_file_that_is_not_liberty(
    tmp_path: Path, require_cpp_core: None
) -> None:
    lib = tmp_path / "bad.lib"
    lib.write_text("library (demo) { cell (x) {", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["cell-map", "--liberty", str(lib), "-o", str(tmp_path / "x.json")])
    assert exc.value.code == 2
