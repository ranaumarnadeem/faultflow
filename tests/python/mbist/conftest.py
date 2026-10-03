"""The fixture chip inserted (with and without JTAG) and synthesized, once a session:
test_synth.py checks the synthesis, test_jtag_chip.py grades the JTAG one."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.mbist.insert import InsertResult, mbist_insert
from faultflow.mbist.synth import SynthesizedChip, synthesize_inserted
from mbist_chip import CHIP, LIBERTY, chip_copy
from warptap_helpers import skip_unless_warptap


def _synthesize(tmp: Path, result: InsertResult) -> SynthesizedChip:
    return synthesize_inserted(
        result.rtl,
        "chip_top",
        {m.instance: (m.shell, m.collar) for m in result.memories},
        libs=[CHIP / "macros/input_demo_8x16_scn4m.v"],
        liberty=LIBERTY,
        workdir=tmp / "synth",
        top_leaves=None if result.jtag is None else result.jtag.instances(),
    )


@pytest.fixture(scope="session")
def synthesized(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[InsertResult, SynthesizedChip]:
    tmp = tmp_path_factory.mktemp("synth")
    result = mbist_insert(chip_copy(tmp), "chip_top", out=tmp / "out")
    return result, _synthesize(tmp, result)


@pytest.fixture(scope="session")
def synthesized_jtag(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[InsertResult, SynthesizedChip]:
    """The chip inserted with jtag: the TAP and the network are DFT too."""
    skip_unless_warptap()
    tmp = tmp_path_factory.mktemp("synth_jtag")
    spec = chip_copy(tmp, lambda t: t.replace("jtag: false", "jtag: {tck_max_mhz: 10}"))
    result = mbist_insert(spec, "chip_top", out=tmp / "out")
    return result, _synthesize(tmp, result)
