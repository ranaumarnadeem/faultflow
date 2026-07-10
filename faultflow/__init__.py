"""faultflow Python control plane."""

from pathlib import Path


def _read_version() -> str:
    """Return the package version.

    The single source of truth is the top-level ``VERSION`` file. It sits at
    the repo root in a source checkout and is bundled next to the package by
    the Nix wrapper (``nix/faultflow.nix``), so ``parent.parent`` resolves it
    in both layouts. Fall back gracefully if it is ever absent.
    """
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "0+unknown"


__version__ = _read_version()
