"""warptap is optional for faultflow, so the tests that need it skip without it --
unless the run says it has warptap: with FAULTFLOW_REQUIRE_WARPTAP=1, a missing
warptap fails those tests instead, so a broken install or a wrong import path can't
pass as a green suite. Install warptap into the venv with `pip install -e <clone>`."""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Mapping

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


def wsp_program(
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str] | None = None,
    scan_ins: Mapping[int, str] | None = None,
    *,
    top: str = "chip",
    scan_enable: str = "scan_en",
) -> Any:
    """warptap's WSP integrity program for the wrapper record ``wrapper``
    (faultflow.wrap.wsp_icl.program_from_warptap): what ``ff.py wsp`` grades with.
    ``scan_outs`` and ``scan_ins``: each wrapper chain's scan-out and scan-in, by
    default ``wbr_so<k>`` and ``wbr_si<k>`` for each chain of the record's cells."""
    skip_unless_warptap("warptap.wsp_integrity")
    from faultflow.wrap.wsp_icl import program_from_warptap

    chains = sorted({int(c["chain"]) for c in wrapper.get("cells", [])})
    outs = dict(scan_outs) if scan_outs is not None else {}
    outs = outs or {k: f"wbr_so{k}" for k in chains}
    ins = dict(scan_ins) if scan_ins is not None else {k: f"wbr_si{k}" for k in chains}
    return program_from_warptap(
        wrapper, outs, top=top, scan_ins=ins, scan_enable=scan_enable
    )
