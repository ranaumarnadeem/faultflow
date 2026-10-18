"""``[wsp]``: how ``ff.py wsp`` gets the integrity program it grades with -- a program
file (``program``), else one warptap builds from the wrapper's ICL (``margin``,
``exhaustive_opcodes``). faultflow's own builder is retired (``builder``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config

HEAD = "[design]\nnetlist = n.json\ncell_lib = c.json\n"


def _load(tmp_path: Path, body: str = ""):  # type: ignore[no-untyped-def]
    path = tmp_path / "c.ofs"
    path.write_text(HEAD + body, encoding="utf-8")
    return load_config(path, "top").wsp


@pytest.mark.unit
def test_the_defaults_build_the_program_with_warptap(tmp_path: Path) -> None:
    wsp = _load(tmp_path)
    assert wsp.program is None
    assert wsp.margin == 8 and wsp.exhaustive_opcodes is True


@pytest.mark.unit
def test_the_keys_are_read(tmp_path: Path) -> None:
    wsp = _load(
        tmp_path,
        "[wsp]\nprogram = prog.json\nmargin = 12\nexhaustive_opcodes = false\n",
    )
    assert wsp.program is not None and wsp.program.name == "prog.json"
    assert wsp.margin == 12 and wsp.exhaustive_opcodes is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("margin = 1", "margin"),
        # Retired: warptap builds the program, whatever the key says.
        ("builder = faultflow", "builder is retired"),
        ("builder = warptap", "builder is retired"),
    ],
)
def test_a_bad_value_is_refused_naming_the_key(
    tmp_path: Path, body: str, key: str
) -> None:
    with pytest.raises(ConfigError, match=rf"\[wsp\] {key}"):
        _load(tmp_path, f"[wsp]\n{body}\n")
