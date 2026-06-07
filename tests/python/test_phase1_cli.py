from pathlib import Path

import pytest

from faultflow.cli import main


def _config(path: Path) -> None:
    path.write_text(
        """
[design]
netlist = missing.json
cell_lib = cells/osu/osu035.json

[fault_model]
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail

[atpg]
mode = comb
blif = missing.blif
output = missing.test

[report]
threshold = 95.0
""".strip() + "\n",
        encoding="utf-8",
    )


def test_init_requires_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)

    with pytest.raises(SystemExit):
        main(["init", "-c", str(cfg)])


def test_init_creates_top_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.ofs"
    _config(cfg)

    assert main(["init", "--top", "demo", "-c", str(cfg)]) == 0
    assert (tmp_path / "output" / "demo" / "faultflow.sqlite").exists()
