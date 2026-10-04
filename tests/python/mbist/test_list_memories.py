"""list-memories on the fixture chip: every memory instance, how each pin is
connected, and the insertion-file entry it suggests for the unconfigured ones."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from faultflow.mbist.memories import (
    MemoryInstance,
    find_memories,
    render_listing,
    spec_entry,
)
from faultflow.mbist.spec import MbistSpec, load_mbist_spec
from faultflow.mbist.yosys import Elaboration, elaborate

ROOT = Path(__file__).resolve().parents[3]
CHIP = ROOT / "tests/fixtures/mbist_chip"
PATHS = [
    "g_bank[0].u_mem",
    "g_bank[1].u_mem",
    "u_core0.u_mem",
    "u_core1.u_mem",
    "u_mem_top",
]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("yosys") is None, reason="needs Yosys on PATH"),
]


@pytest.fixture(scope="module")
def chip(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]]:
    spec = load_mbist_spec(CHIP / "mbist.yml")
    elab = elaborate(
        spec.design, "chip_top", workdir=tmp_path_factory.mktemp("elaborate")
    )
    memories = find_memories(elab.netlist, "chip_top", spec.memory_patterns)
    return spec, elab, {m.path: m for m in memories}


def test_every_memory_instance_is_found_by_its_path(
    chip: tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]],
) -> None:
    _, elab, memories = chip
    assert list(memories) == PATHS
    assert {m.module for m in memories.values()} == {"input_demo_8x16_scn4m"}
    assert memories["u_core1.u_mem"].parent == "core"
    assert memories["g_bank[1].u_mem"].parent == "chip_top"
    assert elab.problems == ()  # the fixture's design checks clean


def test_pin_connections_are_classified(
    chip: tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]],
) -> None:
    _, _, memories = chip
    core0 = memories["u_core0.u_mem"]
    assert core0.pin("wmask0").constant == 1
    assert core0.pin("csb1").constant == 1
    assert (core0.pin("addr1").constant, core0.pin("addr1").width) == (0, 4)
    assert core0.pin("clk1").same_as == ("clk0",)
    assert (core0.pin("clk0").same_as, core0.pin("clk0").net) == (("clk1",), "clk")
    assert core0.pin("dout1").unread and core0.pin("dout1").net == "dout_port1"
    assert not core0.pin("dout0").unread  # a register reads it
    bank0 = memories["g_bank[0].u_mem"]
    assert bank0.pin("dout1").unconnected
    assert not bank0.pin("dout0").unread  # straight to a chip output
    assert bank0.pin("addr0").net == "addr"


def test_patterns_choose_the_macro_types(
    chip: tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]],
) -> None:
    _, elab, _ = chip
    assert find_memories(elab.netlist, "chip_top", ["sky130_sram_*"]) == ()
    exact = find_memories(elab.netlist, "chip_top", ["input_demo_8x16_scn4m"])
    assert [m.path for m in exact] == PATHS


def test_the_listing_marks_configured_memories_and_suggests_the_rest(
    chip: tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]],
) -> None:
    spec, _, memories = chip
    text = render_listing(list(memories.values()), spec)
    assert (
        "u_core0.u_mem  (input_demo_8x16_scn4m in core)  configured as core0_ram"
        in text
    )
    assert "u_core1.u_mem  (input_demo_8x16_scn4m in core)  not configured" in text
    assert "    clk1    input     1  net clk (same net as clk0)" in text
    assert "    dout1   output    8  unread (net dout_port1)" in text
    entry = spec_entry(memories["u_core1.u_mem"])
    assert "  - name: u_core1_u_mem" in entry
    assert "    tie: {wmask0: 1, csb1: 1, addr1: 0x0}" in entry
    assert "    share_clock: [clk1]" in entry
    assert "    unused_outputs: [dout1]" in entry
    # A configured memory gets no suggestion.
    assert text.count("insertion-file entry:") == 2


def test_a_suggested_entry_loads_once_its_config_is_named(
    chip: tuple[MbistSpec, Elaboration, dict[str, MemoryInstance]], tmp_path: Path
) -> None:
    _, _, memories = chip
    entry = spec_entry(memories["g_bank[1].u_mem"]).replace(
        "<your autoMBIST config for input_demo_8x16_scn4m>",
        str(CHIP / "mbist/sram_demo.yml"),
    )
    path = tmp_path / "mbist.yml"
    path.write_text(
        "design:\n"
        f"  sources: [{CHIP / 'rtl/core.v'}, {CHIP / 'rtl/chip_top.v'}]\n"
        "memories:\n" + entry + "\n",
        encoding="utf-8",
    )
    (memory,) = load_mbist_spec(path).memories
    assert (memory.name, memory.instance) == ("g_bank_1_u_mem", "g_bank[1].u_mem")
    assert memory.tie == (("wmask0", 1), ("csb1", 1), ("addr1", 0))
    assert (memory.share_clock, memory.unused_outputs) == (("clk1",), ("dout1",))


def test_the_cli_lists_the_memories(capsys: pytest.CaptureFixture[str]) -> None:
    from faultflow.cli import main

    spec = str(CHIP / "mbist.yml")
    assert main(["list-memories", "--top", "chip_top", "--spec", spec]) == 0
    out = capsys.readouterr().out
    assert all(path in out for path in PATHS)
    assert (
        main(
            [
                "list-memories",
                "--top",
                "chip_top",
                "--spec",
                spec,
                "--pattern",
                "sky130_sram_*",
            ]
        )
        == 0
    )
    assert "no memory instances" in capsys.readouterr().out


def test_the_tcl_command_lists_the_memories(tmp_path: Path) -> None:
    from faultflow.shell.session import ProjectSession
    from faultflow.shell.tcl_bridge import TclBridge

    bridge = TclBridge(ProjectSession(output_root=tmp_path / "output"))
    text = bridge.call(
        "list_memories", "-top", "chip_top", "-spec", str(CHIP / "mbist.yml")
    )
    assert all(path in str(text) for path in PATHS)
