"""ff.py jtag on the chip mbist-insert built with JTAG, synthesized: its TAP reaches the
network with IJTAG_ACCESS, its control TDRs also clear on the chip reset, and each
shell resets its collar through a reset synchronizer. The program, the chip-reset
pulse and the IDCODE come from the manifest, as ff.py jtag takes them; the X-isolation
proof knows every flop by induction, the program passes, and a fault grade comes out
the same from both initial flop states. (The command end to end, after a scan
campaign, is M5d's.)"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import JtagConfig
from faultflow.control_trace import Flop, Netlist
from faultflow.integrations.autombist import (
    AutombistTestAccess,
    load_autombist_manifest,
)
from faultflow.jtag.command import chip_reset_of, manifest_program
from faultflow.jtag.grade import FaultRow, JtagGradeError, golden_gate, grade_faults
from faultflow.jtag.program import TckProgram
from faultflow.jtag.verify import replay_on_gate_level
from faultflow.jtag.xcheck import (
    ChipReset,
    JtagPorts,
    JtagSetup,
    JtagSetupError,
    analyze,
)
from faultflow.mbist.insert import InsertResult
from faultflow.mbist.synth import SynthesizedChip
from faultflow.runner.runner import _load_core
from mbist_chip import ROOT, SKY130_MODELS, gate_verilog

CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in ("yosys", "iverilog", "vvp")),
        reason="needs Yosys and Icarus Verilog on PATH",
    ),
]


class Graded:
    def __init__(self, result: InsertResult, chip: SynthesizedChip) -> None:
        assert result.manifest is not None and result.jtag is not None
        access = load_autombist_manifest(result.manifest).test_access
        assert access is not None
        self.result, self.chip, self.access = result, chip, access
        self.network = result.jtag.instances()
        core = _load_core()
        assert core is not None
        self.core: Any = core
        self.program: TckProgram = manifest_program(access, JtagConfig())
        self.setup = self.analyze()
        module = json.loads(chip.composed_json.read_text(encoding="utf-8"))
        self.module = module["modules"][chip.top]
        cell_map = json.loads(CELL_MAP.read_text(encoding="utf-8"))
        self.flops: list[Flop] = Netlist(self.module, cell_map).flops()

    def analyze(self, **change: Any) -> JtagSetup:
        return analyze(
            self.core,
            self.chip.composed_json,
            CELL_MAP,
            self.chip.top,
            ports=JtagPorts(),
            holds=change.get("holds", {}),
            unsupported="blackbox",
            blackbox_instances=self.chip.memories,
            chip_reset=change.get("chip_reset", chip_reset_of(self.access)),
        )

    def golden(self, setup: JtagSetup) -> list[int]:
        return golden_gate(
            self.core,
            self.chip.composed_json,
            CELL_MAP,
            self.program,
            setup,
            unsupported="blackbox",
            blackbox=self.chip.memories,
        )

    def under(self, prefixes: list[str]) -> list[Flop]:
        return [
            f for f in self.flops if any(f.instance.startswith(p) for p in prefixes)
        ]


@pytest.fixture(scope="module")
def graded(synthesized_jtag: tuple[InsertResult, SynthesizedChip]) -> Graded:
    if _load_core() is None:
        pytest.skip("needs the C++ core")
    return Graded(*synthesized_jtag)


def test_the_manifest_says_how_to_reach_the_network(graded: Graded) -> None:
    access: AutombistTestAccess = graded.access
    assert (access.network_instruction, access.network_opcode) == ("IJTAG_ACCESS", 12)
    assert graded.program.tap.ijtag_access_opcode == 0b1100
    assert graded.setup.pulse == ChipReset("rst_n", 0)
    assert "rst_n" not in graded.setup.holds and graded.setup.holds["clk"] == 0


def test_every_flop_is_a_tap_flop_or_known(graded: Graded) -> None:
    """The network's flops run on TCK, forced by trst_n (the TDRs' through the AND
    with the chip-reset synchronizer); every shell flop -- reset synchronizer,
    control synchronizers, done delay, collar -- is known through the pulsed reset;
    the memories' outputs are the only X sources."""
    setup = graded.setup
    network = graded.under([f"{i}__" for i in graded.network])
    shells = graded.under([f"{m.instance}__" for m in graded.result.memories])
    assert network and shells
    assert {f.instance for f in network} == set(setup.tap_flops)
    assert {f.instance for f in shells} <= set(setup.frozen_known)
    assert set(setup.tap_flops) | set(setup.frozen_known) == {
        f.instance for f in graded.flops
    }
    memory_outputs = {
        bit
        for memory in graded.chip.memories
        for pin, bits in graded.module["cells"][memory]["connections"].items()
        if graded.module["cells"][memory]["port_directions"][pin] == "output"
        for bit in bits
        if isinstance(bit, int)
    }
    assert set(setup.x_sources) == memory_outputs


def test_the_reset_chain_is_left_ungraded_link_by_link(graded: Graded) -> None:
    """The chip reset stuck inactive, and each synchronizer output stuck inactive,
    leave flops unreset: those, and only at that level."""
    keys = dict(graded.setup.reset_path_faults)
    rst_n = graded.module["ports"]["rst_n"]["bits"][0]
    assert keys[f"net:{rst_n}:stem"] == "sa1"
    shell_qs = {
        f.q for f in graded.under([f"{m.instance}__" for m in graded.result.memories])
    }
    chained = {q for q in shell_qs if f"net:{q}:stem" in keys}
    assert chained, "no collar flop is reset through a known flop"
    assert all(keys[f"net:{q}:stem"] == "sa1" for q in chained)


def test_the_program_passes_and_the_grade_doesnt_depend_on_the_start_state(
    graded: Graded, tmp_path: Path
) -> None:
    """The golden gate, then the gate-level replay -- four-state, every flop X, the
    memories floating -- and a fault grade from all-0 and all-1 flops, which must
    agree fault for fault: every fault on a reset or clock path, and a sample of the
    rest."""
    golden = graded.golden(graded.setup)
    replay_on_gate_level(
        gate_verilog(graded.chip.composed_json, tmp_path),
        graded.module,
        graded.chip.top,
        graded.program,
        graded.setup,
        golden,
        models=SKY130_MODELS,
        blackbox=set(graded.chip.memories),
    )

    core = graded.core
    rows = core.list_site_keys(
        str(graded.chip.composed_json), str(CELL_MAP), "blackbox", graded.chip.memories
    )
    faults = [
        FaultRow(
            2 * i + (kind == "sa1"),
            str(row["site_key"]),
            kind,
            int(row["compiled_net_index"]),
        )
        for i, row in enumerate(rows)
        for kind in ("sa0", "sa1")
    ]
    on_paths = {key for key, _ in graded.setup.reset_path_faults}
    for flop in graded.flops:
        for net, consumer, pin, _ in flop.clock.steps:
            on_paths |= {f"net:{net}:stem", f"net:{net}:branch:{consumer}:{pin}"}
    sample = [f for i, f in enumerate(faults) if f.site_key in on_paths or i % 8 == 0]
    grade = grade_faults(
        core,
        graded.chip.composed_json,
        CELL_MAP,
        graded.program,
        graded.setup,
        sample,
        unsupported="blackbox",
        blackbox=graded.chip.memories,
        sim_threads=4,
    )
    assert grade.reset_path and grade.detected
    assert grade.graded == len(sample) - len(grade.reset_path)


def test_without_the_pulse_the_proof_or_the_program_fails(graded: Graded) -> None:
    """Held inactive, as the TDRs need, the chip reset resets nothing: the collars'
    X reaches tdo through the status TDRs. Held active, the proof passes but the
    control TDRs stay cleared, and their write-readback fails."""
    with pytest.raises(JtagSetupError, match="unknown value can reach tdo"):
        graded.analyze(holds={"rst_n": 1}, chip_reset=None)
    held = graded.analyze(chip_reset=None)
    assert held.holds["rst_n"] == 0
    with pytest.raises(JtagGradeError, match=r"in test open_sib_\w+_test_mode"):
        graded.golden(held)
