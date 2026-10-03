"""Grading stuck-at faults with a TCK program played through the TAP, compared at TDO.

Each TCK period is two simulator cycles: TCK low (TMS/TDI/TRST_N applied, TDO sampled
on a shift period -- before the rising edge, as a tester strobes it), then TCK high (the
rising edge). Every other input is held (:mod:`faultflow.jtag.xcheck` settles the holds
and proves TDO X-free), except a chip reset the setup pulses: active through the
program's TRST lead-in, inactive after it.

The golden gate comes first: the netlist's own fault-free TDO must match the program on
every care bit, and be the same from all-0 and all-1 initial flop states. Faults are
then graded on every shifted bit, don't-care or not, against the netlist's own
fault-free TDO -- as a scan unload is -- from both initial states, which must agree
fault for fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Sequence

from faultflow.jtag.program import TckProgram
from faultflow.jtag.xcheck import JtagSetup


class JtagGradeError(RuntimeError):
    """The golden gate failed, or grading contradicted the X-isolation proof."""


class FaultRow(NamedTuple):
    id: int
    site_key: str
    fault_type: str  # "sa0" | "sa1"
    compiled_net_index: int


@dataclass(frozen=True)
class JtagGrade:
    graded: int
    detected: dict[
        int, tuple[str, int]
    ]  # fault id -> (test, TCK period) first detecting
    reset_path: tuple[int, ...]  # fault ids left ungraded: they can keep a flop unreset


def trst_lead_in(program: TckProgram) -> int:
    """The program's TRST lead-in: how many TCK periods it starts with TRST_N low."""
    return len(program.trst_n) - len(program.trst_n.lstrip("0"))


def driven_columns(program: TckProgram, setup: JtagSetup) -> dict[str, str]:
    """Each input the program drives, by port bit, with its value in every TCK period
    (``0``/``1``): TMS, TDI, TRST_N, and the chip reset if the setup pulses it."""
    ports = setup.ports
    columns = {
        ports.tms: program.tms,
        ports.tdi: program.tdi,
        ports.trst_n: program.trst_n,
    }
    pulse = setup.pulse
    if pulse is not None:
        lead = trst_lead_in(program)
        if lead == 0:
            raise JtagGradeError(
                f"the TCK program doesn't start with TRST_N low: the chip reset "
                f"{pulse.port} is pulsed during that lead-in"
            )
        active, inactive = str(pulse.active), str(1 - pulse.active)
        columns[pulse.port] = active * lead + inactive * (len(program) - lead)
    return columns


def sim_cycles(
    program: TckProgram, setup: JtagSetup
) -> tuple[list[list[bool]], list[bool]]:
    """The simulator's input rows (``setup.input_order``) and sample flags."""
    index = {name: i for i, name in enumerate(setup.input_order)}
    base = [False] * len(setup.input_order)
    for name, value in setup.holds.items():
        base[index[name]] = bool(value)
    columns = [
        (index[name], bits) for name, bits in driven_columns(program, setup).items()
    ]
    tck = index[setup.ports.tck]
    cycles: list[list[bool]] = []
    sample: list[bool] = []
    for period in range(len(program)):
        row = list(base)
        for position, bits in columns:
            row[position] = bits[period] == "1"
        high = list(row)
        high[tck] = True
        cycles += [row, high]
        sample += [program.shift[period] == "1", False]
    return cycles, sample


def _simulate(
    core: Any,
    netlist: str | Path,
    cell_map: str | Path,
    program: TckProgram,
    setup: JtagSetup,
    faults: list[tuple[int, int]],
    unsupported: str,
    blackbox: Sequence[str],
    sim_threads: int,
    reference: bool,
    initial: bool,
) -> dict[str, Any]:
    cycles, sample = sim_cycles(program, setup)
    result: dict[str, Any] = core.simulate_sequence_faults(
        str(netlist),
        str(cell_map),
        list(setup.input_order),
        cycles,
        sample,
        [setup.ports.tdo],
        faults,
        unsupported,
        list(blackbox),
        sim_threads,
        reference,
        initial,
    )
    return result


def golden_gate(
    core: Any,
    netlist: str | Path,
    cell_map: str | Path,
    program: TckProgram,
    setup: JtagSetup,
    *,
    unsupported: str,
    blackbox: Sequence[str] = (),
) -> list[int]:
    """The netlist's fault-free TDO at each shift. Raises :class:`JtagGradeError` if it
    depends on the initial flop state or disagrees with the program on a care bit."""
    runs = [
        _simulate(
            core,
            netlist,
            cell_map,
            program,
            setup,
            [],
            unsupported,
            blackbox,
            1,
            False,
            init,
        )
        for init in (False, True)
    ]
    zero = [int(row[0]) for row in runs[0]["golden"]]
    one = [int(row[0]) for row in runs[1]["golden"]]
    periods = program.shift_periods()
    for sample, (a, b) in enumerate(zip(zero, one)):
        if a != b:
            period = periods[sample]
            raise JtagGradeError(
                f"TDO at TCK period {period} (test {program.test_of(period)}) "
                "depends on the flops' initial state"
            )
    for sample, period in enumerate(periods):
        if program.care[period] == "1" and zero[sample] != int(program.tdo[period]):
            raise JtagGradeError(
                f"the netlist's TDO disagrees with the TCK program in test "
                f"{program.test_of(period)} at TCK period {period}: the program "
                f"expects {program.tdo[period]}, the netlist gives {zero[sample]}"
            )
    return zero


def grade_faults(
    core: Any,
    netlist: str | Path,
    cell_map: str | Path,
    program: TckProgram,
    setup: JtagSetup,
    faults: Sequence[FaultRow],
    *,
    unsupported: str,
    blackbox: Sequence[str] = (),
    sim_threads: int = 1,
    reference: bool = False,
) -> JtagGrade:
    """Grade ``faults`` (after :func:`golden_gate` passed): the first TDO sample at
    which each differs from the fault-free machine. ``reference`` grades each with the
    scalar simulator instead (slow; for checking)."""
    graded = [
        f for f in faults if (f.site_key, f.fault_type) not in setup.reset_path_faults
    ]
    skipped = tuple(
        f.id for f in faults if (f.site_key, f.fault_type) in setup.reset_path_faults
    )
    specs = [(f.compiled_net_index, 0 if f.fault_type == "sa0" else 1) for f in graded]
    runs = [
        _simulate(
            core,
            netlist,
            cell_map,
            program,
            setup,
            specs,
            unsupported,
            blackbox,
            sim_threads,
            reference,
            init,
        )
        for init in (False, True)
    ]
    if runs[0]["first_sample"] != runs[1]["first_sample"]:
        diverging = next(
            f.site_key
            for f, a, b in zip(graded, runs[0]["first_sample"], runs[1]["first_sample"])
            if a != b
        )
        raise JtagGradeError(
            f"fault {diverging}'s detection depends on the flops' initial state, which "
            "the X-isolation proof rules out"
        )
    periods = program.shift_periods()
    detected: dict[int, tuple[str, int]] = {}
    for fault, first in zip(graded, runs[0]["first_sample"]):
        if first >= 0:
            period = periods[first]
            detected[fault.id] = (program.test_of(period), period)
    return JtagGrade(graded=len(graded), detected=detected, reset_path=skipped)
