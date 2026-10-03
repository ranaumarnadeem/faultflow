"""The BIST program mbist-insert writes with JTAG: an IEEE 1687 PDL procedure that
runs every memory's BIST over the IJTAG network and checks its result, and the same
procedure retargeted to TCK and clock vectors by warptap, so the two can't disagree.

The schedule's steps run one after another (by default one memory per step: the
lowest peak power); a step's memories run together:

  test_mode = 1, each memory of the step
  bist_start = 1, each                    (a later Update-DR than its test_mode)
  iRunLoop -sck <clock>  the step's longest BIST + the clk latency terms
  read bist_done = 1, each
  read bist_fail = 0, each                (a later capture than its done)
  bist_start = 0, then test_mode = 0, each

Every write and read is applied on its own: warptap retargets one instrument per
iApply, so a step's memories start a few TCK scans apart, and its run loop, which
follows the last start, covers every one of them.

Every latency the inserted logic adds is a named term, never absorbed in a margin:
- clk, in each run loop: CONTROL_SYNC, bist_start crossing into the memory's clock;
  DONE_DELAY, the shell's delay on done; RESET_RELEASE, the collar's reset releasing
  after the chip reset -- start is level-sensitive, so a start written while the
  collar is still in reset waits for the release.
- TCK, in the TAP walk: STATUS_SYNC edges before a capture after a run (a status bit
  crosses into TCK through two flops), and TDR_CLEAR edges after the chip reset
  releases before the first Update-DR (the control TDRs stay cleared until then).
  The walk is checked and padded with Run-Test/Idle cycles where it's too short.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from faultflow.mbist.netlist import InsertError

# clk cycles, per run loop.
RESET_RELEASE = 2
CONTROL_SYNC = 2
DONE_DELAY = 2
CLK_TERMS = RESET_RELEASE + CONTROL_SYNC + DONE_DELAY
# TCK edges.
STATUS_SYNC = 2
TDR_CLEAR = 2

# The collar ports the program drives and checks.
CONTROLS = ("test_mode", "bist_start")
STATUSES = ("bist_done", "bist_fail")


@dataclass(frozen=True)
class ProgramMemory:
    """A memory the program runs: its instruments (collar port -> instrument),
    the chip input its clock comes from, and its BIST's length from start to done
    in cycles of that clock (an upper bound: autoMBIST's)."""

    name: str
    instruments: Mapping[str, str]
    clock: str
    bist_cycles: int


@dataclass(frozen=True)
class Step:
    """One PDL command: "write"/"read" an instrument's value, "apply", or
    "runloop" `count` cycles of `clock`. `memory` is the memory it's for."""

    kind: str
    instrument: str | None = None
    value: int | None = None
    count: int | None = None
    clock: str | None = None
    memory: str | None = None


def run_steps(
    memories: Sequence[ProgramMemory], schedule: Sequence[Sequence[str]]
) -> list[Step]:
    """The run_mbist procedure for `memories`, in the schedule's steps."""
    by_name = {m.name: m for m in memories}
    for m in memories:
        missing = [p for p in (*CONTROLS, *STATUSES) if p not in m.instruments]
        if missing:
            raise InsertError(f"memory {m.name!r}: the collar has no {missing}")
    steps: list[Step] = []
    apply = Step("apply")

    def each(group: list[ProgramMemory], kind: str, port: str, value: int) -> None:
        for m in group:
            steps.append(Step(kind, m.instruments[port], value, memory=m.name))
            steps.append(apply)

    for names in schedule:
        group = [by_name[n] for n in names]
        clocks = sorted({m.clock for m in group})
        if len(clocks) != 1:
            raise InsertError(
                f"the schedule step {list(names)} runs memories clocked by {clocks}: "
                "one run loop counts one clock, so put them in separate steps"
            )
        each(group, "write", "test_mode", 1)
        each(group, "write", "bist_start", 1)
        steps.append(
            Step(
                "runloop",
                count=max(m.bist_cycles for m in group) + CLK_TERMS,
                clock=clocks[0],
            )
        )
        each(group, "read", "bist_done", 1)
        each(group, "read", "bist_fail", 0)
        each(group, "write", "bist_start", 0)
        each(group, "write", "test_mode", 0)
    return steps


@dataclass(frozen=True)
class Vector:
    """One TCK period ("tck": TMS and TDI driven, TDO sampled before the rising
    edge and checked against `tdo` for the `read`-th iRead, 0 = not checked), or
    `count` rising edges of `clock` with TCK stopped in Run-Test/Idle ("run")."""

    kind: str
    tms: int = 0
    tdi: int = 0
    tdo: int = 0
    read: int = 0
    count: int = 0
    clock: str = ""
    state: Any = None  # the TAP state during the period ("tck")


def retarget(
    graph: Any,
    root: Any,
    steps: Sequence[Step],
    *,
    opcode: int,
    ir_width: int = 4,
) -> list[Vector]:
    """`steps` as vectors through the network (warptap's PDLInterpreter over
    `graph`/`root`), from Run-Test/Idle with the chip reset just released: the
    network's instruction (`opcode`) loaded first, then the procedure, the TAP
    walk padded to the TCK latency terms."""
    # warptap is optional: without it, mypy finds no module to check against.
    from warptap.pdl_interpreter import (  # type: ignore[import-not-found]
        PDLInterpreter,
    )
    from warptap.tap_fsm import TapState, next_state  # type: ignore[import-not-found]
    from warptap.tap_integrity import (  # type: ignore[import-not-found]
        select_instruction,
    )
    from warptap.tap_ir import (  # type: ignore[import-not-found]
        GotoState,
        PulsePin,
        Runtest,
        ShiftDR,
        ShiftIR,
        bits_from_int,
    )
    from warptap.tap_ir_play import (  # type: ignore[import-not-found]
        navigation_tms,
        shift_tms,
    )

    pdl = PDLInterpreter(graph, root)
    for step in steps:
        if step.kind in ("write", "read"):
            assert step.instrument is not None and step.value is not None
            pdl.iTarget(step.instrument)
            (pdl.iWrite if step.kind == "write" else pdl.iRead)(step.value)
        elif step.kind == "apply":
            pdl.iApply()
        elif step.kind == "runloop":
            assert step.count is not None
            pdl.iRunLoop(step.count, sck_port=step.clock)
        else:
            raise ValueError(f"unknown step {step.kind!r}")
    ops = [*select_instruction(opcode, ir_width=ir_width), *pdl.program]

    vectors: list[Vector] = []
    state = TapState.RUN_TEST_IDLE
    reads = 0

    def tck(tms: int, tdi: int = 0, tdo: int = 0, read: int = 0) -> None:
        nonlocal state
        vectors.append(Vector("tck", tms, tdi, tdo, read, state=state))
        state = next_state(state, tms)

    for op in ops:
        if isinstance(op, GotoState):
            for tms in navigation_tms(state, op.state):
                tck(tms)
        elif isinstance(op, (ShiftIR, ShiftDR)):
            tdi = bits_from_int(op.tdi, op.bits)
            expected, care, read = [0] * op.bits, [0] * op.bits, 0
            if isinstance(op, ShiftDR) and op.tdo is not None:
                reads += 1
                read = reads
                expected = bits_from_int(op.tdo, op.bits)
                mask = (1 << op.bits) - 1 if op.mask is None else op.mask
                care = bits_from_int(mask, op.bits)
            for i, tms in enumerate(shift_tms(op.bits)):
                tck(tms, tdi[i], expected[i] if care[i] else 0, read if care[i] else 0)
        elif isinstance(op, Runtest):
            for _ in range(op.count):
                tck(0)
        elif isinstance(op, PulsePin):
            if state is not TapState.RUN_TEST_IDLE:
                raise InsertError(f"a run loop outside Run-Test/Idle ({state})")
            vectors.append(Vector("run", count=op.count, clock=op.port))
        else:
            raise InsertError(f"can't retarget {op!r} to vectors")
    if state is not TapState.RUN_TEST_IDLE:
        raise InsertError(f"the program ends in {state}, not Run-Test/Idle")
    expected_reads = sum(1 for s in steps if s.kind == "read")
    if reads != expected_reads:
        raise InsertError(f"{reads} checked scans for {expected_reads} reads")
    return _padded(vectors, TapState)


def _padded(vectors: list[Vector], tap_state: Any) -> list[Vector]:
    """`vectors` with Run-Test/Idle periods added where the TAP walk is shorter
    than a TCK latency term: before the first Update-DR (TDR_CLEAR edges from the
    start) and before the first Capture-DR after each run (STATUS_SYNC edges)."""
    idle = Vector("tck", state=tap_state.RUN_TEST_IDLE)
    out: list[Vector] = []
    # Edges since the start (None once an Update-DR was reached) or since a run.
    since_start: int | None = 0
    since_run: int | None = None
    for vector in vectors:
        if vector.kind == "run":
            out.append(vector)
            since_run = 0
            continue
        if since_start is not None and vector.state is tap_state.UPDATE_DR:
            # At the start, where the TAP is in Run-Test/Idle (no run comes first).
            out[0:0] = [idle] * max(0, TDR_CLEAR - since_start)
            since_start = None
        if since_run is not None and vector.state is tap_state.CAPTURE_DR:
            # The padding goes right after the run, where the TAP is in Run-Test/Idle.
            short = max(0, STATUS_SYNC - since_run)
            at = len(out) - since_run
            out[at:at] = [idle] * short
            since_run = None
        out.append(vector)
        if since_start is not None:
            since_start += 1
        if since_run is not None:
            since_run += 1
    return out


def render_vectors(vectors: Sequence[Vector], clocks: Sequence[str]) -> str:
    """One line per vector: `0 <tms> <tdi> <tdo> <read>` for a TCK period, `1
    <count> <clock> 0 0` for a run, the clock as its index in `clocks`."""
    lines = []
    for v in vectors:
        if v.kind == "run":
            lines.append(f"1 {v.count} {clocks.index(v.clock)} 0 0")
        else:
            lines.append(f"0 {v.tms} {v.tdi} {v.tdo} {v.read}")
    return "\n".join(lines) + "\n"


def _binary(value: int, width: int) -> str:
    return "0b" + format(value, f"0{width}b")


def render_pdl(
    top: str,
    steps: Sequence[Step],
    *,
    schedule: Sequence[Sequence[str]],
    widths: Mapping[str, int],
    opcode: int,
    clocks: Sequence[str],
    icl: str | None = None,
    bsdl: str | None = None,
    vectors: str | None = None,
    bound: str = "autoMBIST's bound on each BIST's length",
) -> str:
    """The run_mbist procedure as IEEE 1687 PDL, each instrument addressed by its
    ICL data register."""
    from warptap.icl_emit import (  # type: ignore[import-not-found]
        INSTRUMENT_MODULE_PREFIX,
    )

    lines = [
        f"# IEEE 1687 PDL: run the memory BISTs of {top} and check each result.",
        "# Generated by FaultFlow mbist-insert.",
    ]
    if icl:
        lines.append(f"# ICL: {icl}")
    if bsdl:
        lines.append(f"# BSDL: {bsdl}")
    lines += [
        "# The IJTAG network is selected by IJTAG_ACCESS "
        f"(IR {_binary(opcode, 4)}), which the",
        "# ICL's AccessLink names; a retargeting tool loads it. Run after TRST with",
        f"# the chip reset released, at least {TDR_CLEAR} TCK edges before the first",
        "# Update-DR (the control TDRs stay cleared until then).",
        "# Schedule, one step after another: "
        + "; ".join(", ".join(step) for step in schedule)
        + ".",
        f"# Each run loop counts {bound}, plus {RESET_RELEASE} clk",
        f"# cycles of reset release, {CONTROL_SYNC} of control synchronizer and",
        f"# {DONE_DELAY} of done delay. Every status capture after a run comes at",
        f"# least {STATUS_SYNC} TCK edges later.",
    ]
    if vectors:
        lines += [
            f"# {vectors}: the same procedure as vectors, one line each:",
            "# '0 <tms> <tdi> <tdo> <read>' for a TCK period (TDO sampled before the",
            "# rising edge, checked for the read-th iRead, 0 = not checked), and",
            "# '1 <count> <clock> 0 0' for count rising edges of a clock with TCK",
            "# stopped in Run-Test/Idle; clocks: "
            + ", ".join(f"{i} = {c}" for i, c in enumerate(clocks))
            + ".",
        ]
    lines += ["", f"iProcsForModule {top}", "", "iProc run_mbist {} {"]
    for step in steps:
        if step.kind in ("write", "read"):
            assert step.instrument is not None and step.value is not None
            register = f"{INSTRUMENT_MODULE_PREFIX}{step.instrument}.DR"
            command = "iWrite" if step.kind == "write" else "iRead"
            value = _binary(step.value, widths[step.instrument])
            lines.append(f"    {command} {register} {value}")
        elif step.kind == "apply":
            lines.append("    iApply")
        elif step.kind == "runloop":
            lines.append(f"    iRunLoop {step.count} -sck {step.clock}")
    lines += ["}", ""]
    return "\n".join(lines)


def clock_port(
    modules: Mapping[str, dict[str, Any]],
    chain: Sequence[tuple[str, str]],
    bits: Sequence[Any],
) -> str | None:
    """The chip input `bits` (a net in the module holding the last cell of
    `chain`) comes from: followed up through the port of each module on the way,
    to an input port of the top. None when it comes from logic instead."""
    for i in range(len(chain) - 1, -1, -1):
        module = modules[chain[i][0]]
        port = next(
            (
                name
                for name, p in module.get("ports", {}).items()
                if p.get("direction") == "input"
                and list(p.get("bits", [])) == list(bits)
            ),
            None,
        )
        if port is None:
            return None
        if i == 0:
            return port
        parent, cell = chain[i - 1]
        bits = modules[parent]["cells"][cell]["connections"].get(port, [])
    return None
