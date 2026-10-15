"""The synthesized continuous-flow decompressor loads what its model says
(faultflow.scan.ring_generator.continuous_scan_in_bits): a core's chains, behind
the decompressor insert_continuous_compression composes, are loaded through the
channels -- one word a shift, after a clock with the scan enable low -- and then
shifted out, in FaultFlow's own simulator and in iverilog on the sky130 cells."""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from typing import Any

import pytest

from continuous_chip import CELL_MAP, COMPOSED_TOP, compose, top_module
from faultflow.runner.runner import _atpg_pi_names
from faultflow.scan.compression import ContinuousDecompressor
from faultflow.scan.ring_generator import continuous_scan_in_bits
from faultflow.scan.techmap import write_scan_techmap
from faultflow.scan.tester_program import Chip, Cycle
from faultflow.scan.yosys import run_scan_techmap
from scan_replay import bench, differences, run


def _channel_names(decompressor: ContinuousDecompressor) -> list[str]:
    if decompressor.num_channels == 1:
        return [decompressor.channel_port]
    return [
        f"{decompressor.channel_port}[{c}]" for c in range(decompressor.num_channels)
    ]


def _expected_unload(
    decompressor: ContinuousDecompressor,
    chain_lengths: list[int],
    words: list[list[bool]],
) -> list[list[bool]]:
    """``out[k][chain]``: the chain's scan-out at unload shift k after ``words``
    were shifted in -- the cell at position length-1-k, which took the scan-in of
    shift len(words)-length+k (False once k is past the chain's length)."""
    scan_ins = continuous_scan_in_bits(
        decompressor.polynomial,
        decompressor.phase_shifter_taps,
        decompressor.num_channels,
        words,
    )
    shifts = len(words)
    return [
        [
            scan_ins[shifts - length + k][chain] if k < length else False
            for chain, length in enumerate(chain_lengths)
        ]
        for k in range(max(chain_lengths))
    ]


def _words(
    decompressor: ContinuousDecompressor, chain_lengths: list[int], seed: int
) -> list[list[bool]]:
    """A load's channel words: warm-up plus the longest chain's shifts."""
    rng = random.Random(seed)
    shifts = decompressor.warmup + max(chain_lengths)
    return [
        [rng.random() < 0.5 for _ in range(decompressor.num_channels)]
        for _ in range(shifts)
    ]


CASES = [(1, [3, 2, 3]), (3, [3, 2, 3, 1]), (8, [2, 3])]


@pytest.mark.integration
@pytest.mark.parametrize("channels,chain_lengths", CASES)
def test_the_synthesized_decompressor_loads_what_the_model_says(
    tmp_path: Path, require_cpp_core: None, channels: int, chain_lengths: list[int]
) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("yosys is not available")
    import _faultflow_core as core  # type: ignore[import-not-found]

    composed, decompressor = compose(tmp_path, chain_lengths, channels)
    inputs = _atpg_pi_names(composed, COMPOSED_TOP)
    channel_names = _channel_names(decompressor)
    assert set(channel_names) <= set(inputs)
    observe = [f"scan_out_{k}" for k in range(len(chain_lengths))]
    for seed in range(4):
        load = _words(decompressor, chain_lengths, seed)
        # The load alone, then launching on shift: one more shift.
        for words in (load, load + [[True] * channels]):
            cycles: list[list[bool]] = []
            sampled: list[bool] = []

            def pulse(scan_en: bool, word: list[bool], sample: bool) -> None:
                for clock in (False, True):
                    values = {"CLK": clock, "scan_en": scan_en, "D": False}
                    values.update(zip(channel_names, word))
                    cycles.append([bool(values.get(n, False)) for n in inputs])
                    sampled.append(sample and not clock)

            zeros = [False] * channels
            pulse(False, zeros, False)
            for word in words:
                pulse(True, word, False)
            for _ in range(max(chain_lengths)):
                pulse(True, zeros, True)
            result = core.simulate_sequence_faults(
                str(composed),
                str(CELL_MAP),
                inputs,
                cycles,
                sampled,
                observe,
                [],
                "fail",
                [],
                0,
            )
            expected = _expected_unload(decompressor, chain_lengths, words)
            for k, row in enumerate(result["golden"]):
                for chain, value in enumerate(row):
                    if k < chain_lengths[chain]:
                        assert bool(value) == expected[k][chain], (seed, k, chain)


@pytest.mark.integration
def test_on_the_sky130_cells_it_loads_the_same(tmp_path: Path) -> None:
    if shutil.which("yosys") is None or shutil.which("iverilog") is None:
        pytest.skip("yosys and iverilog are needed")
    chain_lengths = [3, 2, 3, 1]
    composed, decompressor = compose(tmp_path, chain_lengths, 3)
    techmap = write_scan_techmap(tmp_path / "techmap.v")
    netlist = run_scan_techmap(
        generic_json=composed,
        techmap_verilog=techmap,
        output_verilog=tmp_path / "compressed.v",
        top=COMPOSED_TOP,
        log_path=tmp_path / "techmap.log",
        script_path=tmp_path / "techmap.ys",
    )
    outs = tuple(f"scan_out_{k}" for k in range(len(chain_lengths)))
    chip = Chip(
        top=COMPOSED_TOP,
        module=top_module(composed),
        netlist=netlist,
        instance_prefix="core_inst__",
        clocks=("CLK",),
        scan_enable="scan_en",
        scan_ins=(),
        scan_outs=outs,
        channel_ins=(),
        channel_bits=(),
        fanout=(),
        chain_lengths=dict(enumerate(chain_lengths)),
        max_chain_length=max(chain_lengths),
    )
    channel_names = _channel_names(decompressor)
    zeros = {name: 0 for name in channel_names}
    program: list[Cycle] = []

    def cycle(
        pattern: int, phase: str, scan_en: int, word: dict[str, int], expect: Any
    ) -> None:
        program.append(
            Cycle(
                pattern=pattern,
                phase=phase,
                inputs={"scan_en": scan_en, "D": 0, **word},
                expect=expect,
            )
        )

    for pattern in range(3):
        load = _words(decompressor, chain_lengths, pattern)
        for words in (load, load + [[False, True, True]]):
            cycle(pattern, "preamble", 0, zeros, {})
            for word in words:
                cycle(pattern, "load", 1, dict(zip(channel_names, map(int, word))), {})
            expected = _expected_unload(decompressor, chain_lengths, words)
            for k in range(max(chain_lengths)):
                cycle(
                    pattern,
                    "unload",
                    1,
                    zeros,
                    {
                        out: (
                            int(expected[k][chain])
                            if k < chain_lengths[chain]
                            else None
                        )
                        for chain, out in enumerate(outs)
                    },
                )
    lines = run(chip, bench(chip, program), tmp_path / "iverilog", set())
    assert differences(program, lines) == []
