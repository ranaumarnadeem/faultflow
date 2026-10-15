"""The functional check of a composed chip's continuous-flow decompressor
(faultflow.scan.decompressor_check): it passes what insert_continuous_compression
synthesizes -- alone, and inside the compactor's wrapper -- and catches a wrong
tap, a missing injector, a missing clear and a non-linear gate."""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from pathlib import Path
from typing import Callable

import pytest

from continuous_chip import (
    CELL_MAP,
    COMPOSED_TOP,
    LIBERTY,
    TOP,
    cell_map,
    compose,
    scanned_core,
    top_module,
)
from faultflow.scan.compaction import insert_compaction
from faultflow.scan.compression import ContinuousDecompressor
from faultflow.scan.decompressor_check import (
    Value,
    _anf,
    check_composed_decompressor,
    check_continuous_decompressor,
)

CASES = [(1, [3, 2, 3]), (3, [3, 2, 3, 1]), (8, [2, 3])]


def _shifts(decompressor: ContinuousDecompressor, chain_lengths: list[int]) -> int:
    """A load's shifts and one more, launching on shift."""
    return decompressor.warmup + max(chain_lengths) + 1


def test_the_algebraic_normal_form_finds_the_affine_functions() -> None:
    xor = 0b0110
    assert _anf(xor, 2) == 0b0110  # a + b
    assert _anf(0b1000, 2) == 0b1000  # a.b
    assert Value(((1, 0), (2, 0)), xor).affine() == (3, 0)
    assert Value(((1, 0), (2, 0)), 0b1001).affine() == (3, 1)  # xnor
    assert Value(((1, 0), (2, 0)), 0b1000).affine() is None  # and
    assert Value.of((0, 1)).affine() == (0, 1)
    assert Value.of((5, 1)).affine() == (5, 1)


@pytest.mark.integration
@pytest.mark.parametrize("channels,chain_lengths", CASES)
def test_the_check_passes_what_insert_continuous_compression_builds(
    tmp_path: Path, require_cpp_core: None, channels: int, chain_lengths: list[int]
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    composed, decompressor = compose(tmp_path, chain_lengths, channels)
    result = check_continuous_decompressor(
        top_module(composed),
        cell_map(),
        decompressor,
        _shifts(decompressor, chain_lengths),
    )
    assert result.passed, result.errors


@pytest.mark.integration
def test_the_check_passes_it_inside_the_compactors_wrapper(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    chain_lengths = [3, 2, 3, 1]
    _, decompressor = compose(tmp_path, chain_lengths, 3)
    core_json = tmp_path / "core_for_compaction.json"
    core_json.write_text(json.dumps(scanned_core(chain_lengths)), encoding="utf-8")
    compacted, _ = insert_compaction(
        core_json,
        TOP,
        [f"scan_out_{k}" for k in range(len(chain_lengths))],
        2,
        LIBERTY,
        tmp_path / "compacted.json",
        workdir=tmp_path / "work_compacted",
        decompressor=decompressor,
    )
    result = check_continuous_decompressor(
        top_module(compacted, f"{TOP}_compacted"),
        cell_map(),
        decompressor,
        _shifts(decompressor, chain_lengths),
    )
    assert result.passed, result.errors


def _check(
    tmp_path: Path,
    *,
    built: ContinuousDecompressor | None = None,
    mutate: Callable[[str], str] | None = None,
) -> list[str]:
    """The check's errors on a chip whose decompressor was built from ``built``
    or the RTL ``mutate`` edits, against the decompressor it should be."""
    chain_lengths = [3, 2, 3, 1]
    composed, decompressor = compose(
        tmp_path, chain_lengths, 3, built=built, mutate=mutate
    )
    return check_continuous_decompressor(
        top_module(composed),
        cell_map(),
        decompressor,
        _shifts(decompressor, chain_lengths),
    ).errors


@pytest.mark.integration
def test_a_chain_reading_the_wrong_taps_fails(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    _, decompressor = compose(tmp_path / "plain", [3, 2, 3, 1], 3)
    taps = decompressor.phase_shifter_taps
    swapped = dataclasses.replace(
        decompressor, phase_shifter_taps=[taps[1], taps[0], *taps[2:]]
    )
    errors = _check(tmp_path, built=swapped)
    assert any("scan_in_0" in e and "isn't the model's" in e for e in errors), errors
    assert any("scan_in_1" in e for e in errors), errors


@pytest.mark.integration
def test_a_channel_bit_injected_nowhere_fails(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    errors = _check(
        tmp_path,
        mutate=lambda rtl: re.sub(r"= tdi\[1\];", "= 1'b0;", rtl, count=1),
    )
    assert errors and all("isn't the model's" in e for e in errors), errors


@pytest.mark.integration
def test_a_ring_never_cleared_fails(tmp_path: Path, require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    errors = _check(
        tmp_path,
        mutate=lambda rtl: re.sub(r"reseed \? \d+'b0 : lfsr_reg", "lfsr_reg", rtl),
    )
    assert any("power-up" in e for e in errors), errors


@pytest.mark.integration
def test_a_non_linear_gate_fails(tmp_path: Path, require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    errors = _check(
        tmp_path,
        mutate=lambda rtl: re.sub(
            r"assign scan_in_0 = (.*);", r"assign scan_in_0 = (\1) & tdi[0];", rtl
        ),
    )
    assert any("isn't linear" in e for e in errors), errors


@pytest.mark.integration
def test_the_manifest_level_check_reads_the_chip_the_entry_records(
    tmp_path: Path, require_cpp_core: None
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    chain_lengths = [3, 2, 3, 1]
    composed, decompressor = compose(tmp_path, chain_lengths, 3)
    entry = {
        **decompressor.manifest_entry(),
        "enabled": True,
        "composed_json": str(composed),
        "composed_top": COMPOSED_TOP,
    }
    manifest = {
        "top": TOP,
        "max_chain_length": max(chain_lengths),
        "compression": entry,
    }
    assert check_composed_decompressor(manifest, CELL_MAP).passed

    # An entry for other taps than the chip has is a failure.
    taps = entry["phase_shifter_taps"]
    swapped = {**entry, "phase_shifter_taps": [taps[1], taps[0], *taps[2:]]}
    wrong = check_composed_decompressor({**manifest, "compression": swapped}, CELL_MAP)
    assert not wrong.passed and "isn't the model's" in wrong.errors[0]

    # Nothing to check without compression; a one-shot entry says to run it again.
    assert check_composed_decompressor({"top": TOP}, CELL_MAP).passed
    off = {**manifest, "compression": {"enabled": False}}
    assert check_composed_decompressor(off, CELL_MAP).passed
    old = {k: v for k, v in entry.items() if k != "architecture"}
    result = check_composed_decompressor({**manifest, "compression": old}, CELL_MAP)
    assert not result.passed and "scan-compress" in result.errors[0]
