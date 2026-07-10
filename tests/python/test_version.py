"""The package version is single-sourced from the top-level VERSION file."""

from pathlib import Path

import faultflow


def test_version_matches_version_file() -> None:
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    assert faultflow.__version__ == version_file.read_text().strip()


def test_version_is_not_the_fallback() -> None:
    # Guards that VERSION was actually found (a source checkout always has it);
    # "0+unknown" would mean the read fell through.
    assert faultflow.__version__ != "0+unknown"
    assert faultflow.__version__.count(".") >= 2
