"""The BIST program mbist-insert writes with JTAG, played in simulation (Icarus
Verilog) against the inserted fixture chip: its vectors, TCK beside the memories'
clock at several speed ratios, the result checked only at TDO, as a tester would.

Each run loop is first measured, then cut to the exact count -- every memory's
own BIST length plus the named latency terms, no margin -- so a missing term fails:
the program passes at TCK:clk ratios of 1:8, 1:1, 8:1 and about 135:1, starting
right after a chip reset; a run loop one clock cycle short fails. The resets are
released just after a clk edge, and at 135:1 the first start is written before the
next one: the collar is still in reset, so the reset-release term counts in full.
The TCK half periods are chosen so no rising TCK edge falls on a clk edge, where
a synchronizer's sampling would be a race in simulation.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.mbist.insert import InsertResult, mbist_insert
from faultflow.mbist.program import (
    CLK_TERMS,
    ProgramMemory,
    render_vectors,
    retarget,
    run_steps,
)
from mbist_chip import MODEL, TCK_HALF, Played, chip_copy, play_jtag_program
from warptap_helpers import skip_unless_warptap

JTAG = "jtag: {tck_max_mhz: 10}"
MEMORIES = ("core0_ram", "bank0_ram", "top_ram")
COLLARS = {
    "core0_ram": "dut.u_core0.u_mem.u_collar",
    "bank0_ram": "dut.\\g_bank[0].u_mem .u_collar",
    "top_ram": "dut.u_mem_top.u_collar",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in ("yosys", "iverilog", "vvp")),
        reason="needs Yosys and Icarus Verilog on PATH",
    ),
]


def _insert(tmp: Path, edit: Any = lambda text: text) -> InsertResult:
    spec = chip_copy(tmp, lambda text: edit(text.replace("jtag: false", JTAG)))
    return mbist_insert(spec, "chip_top", out=tmp / "out")


@pytest.fixture(scope="module")
def inserted(tmp_path_factory: pytest.TempPathFactory) -> InsertResult:
    skip_unless_warptap()
    return _insert(tmp_path_factory.mktemp("bist_program"))


def _play(
    inserted: InsertResult,
    vectors: str,
    work: Path,
    ratio: str = "1:1",
    defparams: str = "",
) -> Played:
    return play_jtag_program(
        [inserted.rtl, MODEL],
        vectors,
        work,
        ratio=ratio,
        defparams=defparams,
        probes=COLLARS,
    )


def _vectors(
    inserted: InsertResult,
    memories: tuple[ProgramMemory, ...],
    schedule: Any = None,
) -> tuple[str, list[tuple[str, str]]]:
    """The program for `memories`, as vectors, and what each read checks (memory,
    port), in read order."""
    assert inserted.jtag is not None
    steps = run_steps(memories, schedule or inserted.schedule)
    vectors = retarget(
        inserted.jtag.graph, inserted.jtag.root, steps, opcode=inserted.jtag.opcode
    )
    ports = {i: p for m in memories for p, i in m.instruments.items()}
    reads = [
        (str(s.memory), ports[str(s.instrument)]) for s in steps if s.kind == "read"
    ]
    return render_vectors(vectors, sorted({m.clock for m in memories})), reads


def _exact(
    inserted: InsertResult, lengths: dict[str, int], short: int = 0
) -> tuple[ProgramMemory, ...]:
    """The memories, each BIST length the measured one (`short` cycles less)."""
    return tuple(
        dataclasses.replace(m, bist_cycles=lengths[m.name] - short)
        for m in inserted.program_memories
    )


@pytest.fixture(scope="module")
def measured(inserted: InsertResult, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The program as written (autoMBIST's bound) passes; it measures each BIST."""
    assert inserted.vectors is not None
    vectors = inserted.vectors.read_text(encoding="utf-8")
    played = _play(inserted, vectors, tmp_path_factory.mktemp("measure"))
    assert played.failures == 0, played.out
    return played.lengths


# --- what the program says ------------------------------------------------------


def test_the_program_runs_each_memory_in_its_own_step(inserted: InsertResult) -> None:
    assert inserted.pdl is not None
    pdl = inserted.pdl.read_text(encoding="utf-8")
    body = pdl.split("iProc run_mbist {} {", 1)[1]
    commands = [line.strip() for line in body.splitlines() if line.strip()][:-1]
    prefix = "warptap_instr_core0_ram"
    assert commands[:12] == [
        f"iWrite {prefix}_test_mode.DR 0b1",
        "iApply",
        f"iWrite {prefix}_bist_start.DR 0b1",
        "iApply",
        f"iRunLoop {420 + CLK_TERMS} -sck clk",
        f"iRead {prefix}_bist_done.DR 0b1",
        "iApply",
        f"iRead {prefix}_bist_fail.DR 0b0",
        "iApply",
        f"iWrite {prefix}_bist_start.DR 0b0",
        "iApply",
        f"iWrite {prefix}_test_mode.DR 0b0",
    ]
    assert sum(c.startswith("iRunLoop") for c in commands) == 3
    assert "IJTAG_ACCESS (IR 0b1100)" in pdl
    assert "BSDL: chip_top_mbist.bsd" in pdl and "ICL: chip_top_mbist.icl" in pdl


def test_the_written_program_passes_with_autombists_bound(measured: dict) -> None:
    # Every BIST ran (and the fixture's are all the same length).
    assert set(measured) == set(MEMORIES)
    assert len(set(measured.values())) == 1 and 0 < measured["core0_ram"] < 420


# --- the exact count, at every speed ratio ------------------------------------------


@pytest.mark.parametrize("ratio", list(TCK_HALF))
def test_the_exact_count_passes_at_every_tck_to_clk_ratio(
    inserted: InsertResult, measured: dict, tmp_path: Path, ratio: str
) -> None:
    vectors, _ = _vectors(inserted, _exact(inserted, measured))
    played = _play(inserted, vectors, tmp_path, ratio)
    assert played.failures == 0, played.out


def test_one_cycle_short_fails_the_first_done_read(
    inserted: InsertResult, measured: dict, tmp_path: Path
) -> None:
    """At 135:1 the first start is written before the memory's clock has ticked
    since the chip reset, while the collar is still in reset (released two clk
    edges later): a run loop one cycle short of the exact count reads done before
    it rises. The later steps start long after, with a cycle to spare."""
    vectors, reads = _vectors(inserted, _exact(inserted, measured, short=1))
    played = _play(inserted, vectors, tmp_path, "135:1")
    assert [reads[r - 1] for r in played.mismatches] == [("core0_ram", "bist_done")]


def test_a_stuck_bit_fails_only_its_own_memory(
    inserted: InsertResult, measured: dict, tmp_path: Path
) -> None:
    vectors, reads = _vectors(inserted, _exact(inserted, measured))
    stuck = "".join(
        f"    defparam {COLLARS['bank0_ram']}.u_sram.{name} = {value};\n"
        for name, value in (("STUCK_ADDR", 7), ("STUCK_BIT", 2), ("STUCK_VALUE", 1))
    )
    played = _play(inserted, vectors, tmp_path, "8:1", stuck)
    assert [reads[r - 1] for r in played.mismatches] == [("bank0_ram", "bist_fail")]


@pytest.mark.parametrize("ratio", ["1:8", "135:1"])
def test_every_memory_together_passes_at_the_exact_count(
    inserted: InsertResult, measured: dict, tmp_path: Path, ratio: str
) -> None:
    """One step for all three: one run loop, after the last start, covers them."""
    vectors, reads = _vectors(
        inserted, _exact(inserted, measured), schedule=(MEMORIES,)
    )
    assert vectors.count("\n1 ") == 1  # one run loop
    played = _play(inserted, vectors, tmp_path, ratio)
    assert played.failures == 0, played.out


# --- two memories with different BIST lengths ---------------------------------------


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> tuple[InsertResult, dict]:
    """core0_ram on a march-x collar (a shorter BIST), the others on march-c."""
    skip_unless_warptap()
    tmp = tmp_path_factory.mktemp("mixed")

    def march_x(text: str) -> str:
        first = text.index("autombist_config: mbist/sram_demo.yml")
        return text[:first] + text[first:].replace(
            "mbist/sram_demo.yml", "mbist/sram_demo_x.yml", 1
        ).replace("algo: march-c", "algo: march-x", 1)

    result = _insert(tmp, march_x)
    assert result.vectors is not None
    played = _play(result, result.vectors.read_text("utf-8"), tmp / "measure")
    assert played.failures == 0, played.out
    return result, played.lengths


def test_each_step_runs_for_its_own_longest_bist(
    mixed: tuple[InsertResult, dict],
) -> None:
    result, lengths = mixed
    bounds = {m.name: m.bist_cycles for m in result.program_memories}
    assert bounds == {"core0_ram": 260, "bank0_ram": 420, "top_ram": 420}
    assert lengths["core0_ram"] < lengths["bank0_ram"] == lengths["top_ram"]
    assert result.pdl is not None
    loops = re.findall(r"iRunLoop (\d+)", result.pdl.read_text("utf-8"))
    assert loops == [str(260 + CLK_TERMS), str(420 + CLK_TERMS), str(420 + CLK_TERMS)]


@pytest.mark.parametrize("schedule", ["sequential", "concurrent"])
def test_different_lengths_pass_at_the_exact_count(
    mixed: tuple[InsertResult, dict], tmp_path: Path, schedule: str
) -> None:
    result, lengths = mixed
    steps = None if schedule == "sequential" else (MEMORIES,)
    vectors, _ = _vectors(result, _exact(result, lengths), schedule=steps)
    played = _play(result, vectors, tmp_path, "135:1")
    assert played.failures == 0, played.out
