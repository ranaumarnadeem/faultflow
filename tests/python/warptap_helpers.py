"""warptap is optional for faultflow, so the tests that need it skip without it --
unless the run says it has warptap: with FAULTFLOW_REQUIRE_WARPTAP=1, a missing
warptap fails those tests instead, so a broken install or a wrong import path can't
pass as a green suite. Install warptap into the venv with `pip install -e <clone>`."""

from __future__ import annotations

import importlib.util
import os

import pytest

REQUIRE_ENV = "FAULTFLOW_REQUIRE_WARPTAP"


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a missing parent package of a dotted name
        return False


def skip_unless_warptap(module: str = "warptap") -> None:
    """Skip the calling test unless `module` (warptap or one of its modules) is
    importable; with FAULTFLOW_REQUIRE_WARPTAP=1, fail it instead."""
    if _importable(module):
        return
    message = f"needs {module} importable (pip install -e <a warptap clone>)"
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{message}; {REQUIRE_ENV}=1 says this run has it")
    pytest.skip(message)
