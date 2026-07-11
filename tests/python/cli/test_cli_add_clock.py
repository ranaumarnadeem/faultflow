from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.config import _parse_clocks
from configparser import ConfigParser


def _base_config(path: Path) -> None:
    path.write_text(
        """
# a hand-written comment that must survive add-clock edits
[design]
netlist = missing.json
cell_lib = cells/sky130/sky130_fd_sc_hd.json

[fault_model]
collapsing = false
""".lstrip(),
        encoding="utf-8",
    )


def _clocks(path: Path) -> tuple:
    parser = ConfigParser()
    parser.read(path)
    return _parse_clocks(parser)


def test_add_clock_creates_section(tmp_path: Path) -> None:
    cfg = tmp_path / "config.ofs"
    _base_config(cfg)
    assert main(["add-clock", "clk_a", "-c", str(cfg)]) == 0

    clocks = _clocks(cfg)
    assert [c.port for c in clocks] == ["clk_a"]
    assert clocks[0].off_state == 0
    assert "a hand-written comment" in cfg.read_text(encoding="utf-8")


def test_add_clock_appends_and_dedupes(tmp_path: Path) -> None:
    cfg = tmp_path / "config.ofs"
    _base_config(cfg)
    assert main(["add-clock", "clk_a", "-c", str(cfg)]) == 0
    assert main(["add-clock", "clk_b", "-c", str(cfg), "--off", "1"]) == 0

    clocks = _clocks(cfg)
    assert {c.port for c in clocks} == {"clk_a", "clk_b"}
    by_port = {c.port: c.off_state for c in clocks}
    assert by_port["clk_a"] == 0
    assert by_port["clk_b"] == 1

    # Redeclaring clk_a with off=1 replaces it in place (dedup by port).
    assert main(["add-clock", "clk_a", "-c", str(cfg), "--off", "1"]) == 0
    clocks = _clocks(cfg)
    assert len(clocks) == 2
    assert {c.port: c.off_state for c in clocks}["clk_a"] == 1


def test_add_clock_rejects_missing_config(tmp_path: Path) -> None:
    missing = tmp_path / "nope.ofs"
    with pytest.raises(SystemExit) as exc:
        main(["add-clock", "clk_a", "-c", str(missing)])
    assert exc.value.code == 2
