"""The cycles a tester applies (faultflow.scan.tester_program), on small manifests:
which phase each cycle is, what it drives, and what it compares. The real-cell
replay (test_silicon_replay) checks the same cycles on the cells of every flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.scan.errors import ScanError
from faultflow.scan.tester_program import chip_of, cycles

CORE_PORTS = {
    "clk": {"direction": "input", "bits": [2]},
    "rst": {"direction": "input", "bits": [3]},
    "d": {"direction": "input", "bits": [4]},
    "scan_en": {"direction": "input", "bits": [5]},
    "scan_in_0": {"direction": "input", "bits": [6]},
    "scan_in_1": {"direction": "input", "bits": [7]},
    "y": {"direction": "output", "bits": [8, 9]},
    "scan_out_0": {"direction": "output", "bits": [10]},
    "scan_out_1": {"direction": "output", "bits": [11]},
}


def _manifest(tmp_path: Path, cells: dict[str, Any] | None = None) -> dict[str, Any]:
    """Two chains, of two flops and one; clock net 2."""
    generic = tmp_path / "core.json"
    module = {"ports": CORE_PORTS, "cells": cells or {}, "netnames": {}}
    generic.write_text(json.dumps({"modules": {"core": module}}), encoding="utf-8")
    return {
        "top": "core",
        "generic_json": str(generic),
        "sky130_verilog": str(tmp_path / "core_scan.v"),
        "clock_nets": [2],
        "scan_enable": "scan_en",
        "scan_inputs": ["scan_in_0", "scan_in_1"],
        "scan_outputs": ["scan_out_0", "scan_out_1"],
        "chains": [{"index": 0, "length": 2}, {"index": 1, "length": 1}],
        "max_chain_length": 2,
    }


def _composed(tmp_path: Path, name: str, ports: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / f"{name}.json"
    module = {"ports": ports, "cells": {}, "netnames": {}}
    path.write_text(json.dumps({"modules": {name: module}}), encoding="utf-8")
    return {"enabled": True, "composed_json": str(path), "composed_top": name}


PATTERN: dict[str, Any] = {
    "load_seqs": {"0": [True, False], "1": [False, True]},
    "capture_pi_values": {"d": True, "rst": True, "y[0]": True, "y[1]": False},
    "expected_unload": {"0": [True, True], "1": [False, False]},
    "unload_mask": {"0": [True, False], "1": [True, True]},
    "shift_pi_values": {"rst": False},
}


def test_a_pattern_is_its_preamble_load_capture_and_unload_in_order(
    tmp_path: Path,
) -> None:
    chip = chip_of(_manifest(tmp_path))
    program = cycles(chip, [{**PATTERN, "preamble_cycles": 1}])
    assert [c.phase for c in program] == [
        "preamble",
        "load",
        "load",
        "capture",
        "unload",
        "unload",
    ]
    assert all(c.pulse and c.pattern == 0 for c in program)
    assert [c.inputs["scan_en"] for c in program] == [0, 1, 1, 0, 1, 1]
    assert [(c.inputs["scan_in_0"], c.inputs["scan_in_1"]) for c in program[1:3]] == [
        (1, 0),
        (0, 1),
    ]
    assert "clk" not in program[0].inputs
    capture = program[3]
    assert capture.expect == {"y[0]": 1, "y[1]": 0}
    # The masked bit -- a flop that captured an unknown value -- isn't compared.
    assert [dict(c.expect) for c in program[4:]] == [
        {"scan_out_0": 1, "scan_out_1": 0},
        {"scan_out_0": None, "scan_out_1": 0},
    ]


def test_shift_values_hold_everywhere_but_the_capture(tmp_path: Path) -> None:
    """Testing the reset, the capture sets it active; it stays inactive while the
    chains shift, and the launch on capture is a capture too."""
    chip = chip_of(_manifest(tmp_path))
    program = cycles(chip, [{**PATTERN, "launch": "loc", "preamble_cycles": 1}])
    assert {c.phase: c.inputs["rst"] for c in program} == {
        "preamble": 0,
        "load": 0,
        "launch": 1,
        "capture": 1,
        "unload": 0,
    }
    assert all(c.inputs["d"] == 1 for c in program)


def test_launch_on_shift_is_one_more_shift_with_its_scan_in_bits(
    tmp_path: Path,
) -> None:
    chip = chip_of(_manifest(tmp_path))
    los = {**PATTERN, "launch": "los", "launch_scan_in": {"0": True, "1": False}}
    (launch,) = [c for c in cycles(chip, [los]) if c.phase == "launch"]
    assert (launch.inputs["scan_en"], launch.inputs["scan_in_0"]) == (1, 1)
    assert (launch.inputs["scan_in_1"], launch.inputs["rst"]) == (0, 0)
    with pytest.raises(ScanError, match="unknown launch"):
        cycles(chip, [{**PATTERN, "launch": "lob"}])


def test_a_compressed_pattern_holds_its_seed_on_the_channels(tmp_path: Path) -> None:
    """Seed bit k on tdi[k] in every cycle; the chain one flop long shows the
    decompressor's bits after its own, so they aren't compared."""
    manifest = _manifest(tmp_path)
    ports = {name: port for name, port in CORE_PORTS.items() if "scan_in" not in name}
    ports["tdi"] = {"direction": "input", "bits": list(range(20, 28))}
    manifest["compression"] = {
        **_composed(tmp_path, "core_compressed", ports),
        "num_channels": 8,
        "channel_port": "tdi",
    }
    chip = chip_of(manifest)
    assert (chip.top, chip.instance_prefix, chip.scan_ins) == (
        "core_compressed",
        "core_inst__",
        (),
    )
    program = cycles(chip, [{**PATTERN, "seed": 0b10000101}])
    for cycle in program:
        assert [cycle.inputs[f"tdi[{k}]"] for k in range(8)] == [
            1,
            0,
            1,
            0,
            0,
            0,
            0,
            1,
        ]
    assert [dict(c.expect) for c in program if c.phase == "unload"] == [
        {"scan_out_0": 1, "scan_out_1": 0},
        {"scan_out_0": None, "scan_out_1": None},
    ]
    with pytest.raises(ScanError, match="has no seed"):
        cycles(chip, [PATTERN])


def test_a_compacted_unload_is_compared_on_the_channels(tmp_path: Path) -> None:
    """Each channel bit is the XOR of the bits it reads, compared only when every
    one is known."""
    manifest = _manifest(tmp_path)
    ports = {name: port for name, port in CORE_PORTS.items() if "scan_out" not in name}
    ports["tdo"] = {"direction": "output", "bits": [30, 31]}
    manifest["compaction"] = {
        **_composed(tmp_path, "core_compacted", ports),
        "channel_port": "tdo",
        "scan_out_ports": ["scan_out_1", "scan_out_0"],
        "fanout": [[0, 1], [1]],
    }
    chip = chip_of(manifest)
    assert chip.scan_outs == () and chip.fanout == ((1, 0), (0,))
    unloads = [dict(c.expect) for c in cycles(chip, [PATTERN]) if c.phase == "unload"]
    assert unloads == [{"tdo[0]": 1, "tdo[1]": 1}, {"tdo[0]": None, "tdo[1]": None}]


def test_wrapper_cells_and_a_chip_compacted_before_compression_are_refused(
    tmp_path: Path,
) -> None:
    wrapped = _manifest(tmp_path, {"u_wbc": {"type": "$wbc_in_scan_faultflow"}})
    with pytest.raises(ScanError, match="IEEE 1500 wrapper cells"):
        chip_of(wrapped)
    manifest = _manifest(tmp_path)
    manifest["compression"] = {"enabled": True}
    manifest["compaction"] = {"enabled": True, "with_decompressor": False}
    with pytest.raises(ScanError, match="run scan-compact again"):
        chip_of(manifest)
