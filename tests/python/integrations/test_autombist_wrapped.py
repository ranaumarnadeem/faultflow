"""Tests for synthesizing a design wrapped for JTAG access from its manifest's
`test_access` block: which modules become blocks, and the check that the
wrapped glue holds exactly the listed instances.

Pure unit tests: Yosys is replaced by a stub that writes the glue netlist a
test hands it. The real-Yosys flow is covered in
`test_autombist_integration.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import faultflow.integrations.autombist as autombist
from faultflow.integrations.autombist import (
    AutombistTestAccess,
    AutombistTestAccessInstance,
    synthesize_wrapped_glue,
    wrapped_block_modules,
)
from faultflow.project.assemble import AssembleError

CONTROLLER = "\\$paramod$abc\\algo_top"


def _instance(path: str, module: str, category: str) -> AutombistTestAccessInstance:
    return AutombistTestAccessInstance(
        category=category,
        hierarchical_path=path,
        hierarchy_hint="blackbox" if category == "memory" else "separate",
        instance_name=path,
        module_type=module,
        sources=(Path("/a/mem_bbox.v"),) if category == "memory" else (),
    )


def _access() -> AutombistTestAccess:
    return AutombistTestAccess(
        top_module="top",
        output_verilog=Path("/a/top_test_access.v"),
        boundary_ports=("tck", "tms", "tdi", "tdo", "trst_n"),
        instances=(
            _instance("u_mem", "mem", "memory"),
            _instance("u_algo", CONTROLLER, "mbist_controller"),
            _instance("warptap_sib_a", "sib_cell", "ijtag_sib"),
            _instance("warptap_sib_b", "sib_cell", "ijtag_sib"),
            _instance("warptap_tap_core", "tap_core", "jtag_tap"),
        ),
        instruments=(),
    )


def test_each_distinct_separate_module_is_one_block() -> None:
    assert wrapped_block_modules(_access()) == (CONTROLLER, "sib_cell", "tap_core")


def test_block_file_names_stay_distinct() -> None:
    stems = autombist._file_stems([CONTROLLER, "$paramod$abc/algo_top", "sib_cell"])
    assert stems[CONTROLLER] == "paramod_abc_algo_top"
    assert stems["$paramod$abc/algo_top"] == "paramod_abc_algo_top_2"
    assert stems["sib_cell"] == "sib_cell"


def _glue(cells: dict[str, str]) -> dict[str, Any]:
    """A glue netlist whose top has `cells` (name -> type), with every design
    module the real glue defines: the blackboxes and the memory."""
    modules: dict[str, Any] = {
        name: {"attributes": {"blackbox": "1"}}
        for name in ("mem", CONTROLLER, "sib_cell", "tap_core")
    }
    modules["top"] = {
        "attributes": {"top": "1"},
        "ports": {},
        "cells": {name: {"type": cell_type} for name, cell_type in cells.items()},
    }
    return {"modules": modules}


def _synthesize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, glue: dict[str, Any]
) -> list[str]:
    script: list[str] = []

    def fake_yosys(lines: list[str], **_: Any) -> None:
        script.extend(lines)
        (tmp_path / "glue.json").write_text(json.dumps(glue), encoding="utf-8")

    monkeypatch.setattr(autombist, "_run_yosys", fake_yosys)
    access = _access()
    synthesize_wrapped_glue(
        access,
        wrapped_block_modules(access),
        liberty=Path("/a/cells.lib"),
        workdir=tmp_path,
    )
    return script


_LISTED = {
    "u_mem": "mem",
    "u_algo": CONTROLLER,
    "warptap_sib_a": "sib_cell",
    "warptap_sib_b": "sib_cell",
    "warptap_tap_core": "tap_core",
}


def test_the_glue_blackboxes_every_block_and_reads_the_memory_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    glue = _glue({**_LISTED, "g_and": "sky130_fd_sc_hd__and2_1"})

    script = _synthesize(tmp_path, monkeypatch, glue)

    assert script[:2] == [
        'read_verilog -sv "/a/top_test_access.v"',
        'read_verilog -lib "/a/mem_bbox.v"',
    ]
    assert script[2:5] == [
        f"blackbox {CONTROLLER}",
        "blackbox sib_cell",
        "blackbox tap_core",
    ]
    assert "hierarchy -check -top top" in script


@pytest.mark.parametrize(
    ("cells", "reported"),
    [
        # A cell of a design module no instance lists.
        ({**_LISTED, "u_extra": "sib_cell"}, "unlisted=['u_extra']"),
        # An unmapped Yosys cell is not library logic either.
        ({**_LISTED, "$auto$1": "$_DFF_P_"}, "unlisted=['$auto$1']"),
        ({k: v for k, v in _LISTED.items() if k != "warptap_sib_b"}, "missing="),
        ({**_LISTED, "warptap_sib_b": "tap_core"}, "wrong_type=['warptap_sib_b']"),
    ],
)
def test_the_glue_must_hold_exactly_the_listed_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cells: dict[str, str],
    reported: str,
) -> None:
    with pytest.raises(AssembleError, match=re.escape(reported)):
        _synthesize(tmp_path, monkeypatch, _glue(cells))
