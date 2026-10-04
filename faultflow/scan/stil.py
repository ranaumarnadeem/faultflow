"""Exported scan patterns as STIL (IEEE 1450): the cycles a tester applies
(``faultflow.scan.tester_program``), each pattern alone or each load overlapping the
unload of the pattern before, written with STIL's scan constructs. Each load and
unload is a call of one ``load_unload`` procedure whose Shift takes every chain's
bits; a preamble is a Loop; a launch and a capture are one vector each.

One WaveformTable: inputs change at the start of the period, outputs are strobed
before the scan clocks rise, and the clocks pulse after the strobe. In STIL every
signal keeps its last waveform character. Readers differ on whether a procedure
starts from its caller's characters and leaves its own, or starts afresh and gives
its caller's back, so the STIL relies on neither: the procedure sets every signal
itself, a call passing the inputs' values, and after a call the writer sets again
every group the next cycle needs. Otherwise a condition statement sets only what
changes. The procedure leaves every output but the scan outputs uncompared (X),
and only the capture vectors and the shifts' scan outputs compare anything.

With scan compression, the scan inputs are the compression channels: a call passes
the pattern's seed, held on them through the shifts, and a character per shift for
each scan output. With compaction, the scan outputs are the compactor's channels.

ScanStructures, describing each chain by its pins and cells, are written only for
a chip without compression or compaction, whose chains are its pins'. They leave
out ScanEnable, which is optional, and which the one independent parser this is
checked with (Semi-ATE-STIL) doesn't take in its standard form."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import Any, Mapping, Sequence

from faultflow.scan.errors import ScanError
from faultflow.scan.tester_program import Chip, Cycle

WAVEFORM_TABLE = "_wft_"
PROCEDURE = "load_unload"
PATTERN = "_pattern_"
BURST = "_burst_"


@dataclass(frozen=True)
class Timing:
    """The WaveformTable's period and edges, in ns."""

    period: int = 100
    strobe: int = 40
    clock_rise: int = 50
    clock_fall: int = 80

    def __post_init__(self) -> None:
        if not 0 < self.strobe < self.clock_rise < self.clock_fall < self.period:
            raise ScanError(
                "STIL timing: the strobe, then the clock's rise and fall, must "
                "come in that order within the period"
            )


def _quoted(name: str) -> str:
    return f'"{name}"'


@dataclass(frozen=True)
class _Groups:
    pi: list[str]
    po: list[str]
    si: list[str]
    so: list[str]
    clk: list[str]
    se: str


def _groups(chip: Chip) -> _Groups:
    si = list(chip.scan_ins or chip.seed_bits)
    so = list(chip.scan_outs or chip.channel_bits)
    clk = list(chip.clocks)
    taken = set(si) | set(clk) | {chip.scan_enable}
    return _Groups(
        pi=[name for name in chip.inputs if name not in taken],
        po=[name for name in chip.outputs if name not in set(so)],
        si=si,
        so=so,
        clk=clk,
        se=chip.scan_enable,
    )


def _drive(cycle: Cycle, names: Sequence[str]) -> str:
    return "".join(str(int(cycle.inputs[name])) for name in names)


def _compare(cycle: Cycle, names: Sequence[str]) -> str:
    def wfc(name: str) -> str:
        value = cycle.expect.get(name)
        return "X" if value is None else "H" if value else "L"

    return "".join(wfc(name) for name in names)


def _assign(pairs: Sequence[tuple[str, str]]) -> str:
    return " ".join(f"{_quoted(group)} = {data};" for group, data in pairs if data)


def write_stil(
    chip: Chip,
    program: Sequence[Cycle],
    *,
    chains: Sequence[Mapping[str, Any]] = (),
    title: str = "",
    timing: Timing = Timing(),
) -> str:
    """`program` (tester_program.cycles on `chip`) as STIL. `chains` are the scan
    manifest's chains, for ScanStructures."""
    groups = _groups(chip)
    lines = ["STIL 1.0;", "", "Header {"]
    lines.append(
        f"    Title {_quoted(title or f'FaultFlow scan patterns: {chip.top}')};"
    )
    overlapped = any(cycle.unloading not in (None, cycle.pattern) for cycle in program)
    applied = (
        "Each load also unloads the pattern before; the preamble is given once, "
        "first, and a last unload ends the patterns."
        if overlapped
        else "Each pattern alone: its preamble, load, launch, capture and unload, "
        "as FaultFlow grades it."
    )
    lines += [
        '    Source "FaultFlow";',
        f"    Ann {{* {applied} *}}",
        "}",
        "",
        "Signals {",
    ]
    lines += [f"    {_quoted(name)} In;" for name in chip.inputs]
    lines += [f"    {_quoted(name)} Out;" for name in chip.outputs]
    lines += ["}", "", "SignalGroups {"]
    named = (
        ("_pi", groups.pi),
        ("_po", groups.po),
        ("_si", groups.si),
        ("_so", groups.so),
        ("_clk", groups.clk),
        ("_se", [groups.se]),
    )
    present = {group: members for group, members in named if members}
    for group, members in present.items():
        expression = " + ".join(_quoted(member) for member in members)
        lines.append(f"    {_quoted(group)} = '{expression}';")
    lines += ["}", "", "Timing {", f"    WaveformTable {_quoted(WAVEFORM_TABLE)} {{"]
    lines += [f"        Period '{timing.period}ns';", "        Waveforms {"]
    for group in ("_pi", "_si", "_se"):
        if group in present:
            lines.append(f"            {_quoted(group)} {{ 01 {{ '0ns' D/U; }} }}")
    lines.append(
        f"            {_quoted('_clk')} {{ 0P {{ '0ns' D; "
        f"'{timing.clock_rise}ns' D/U; '{timing.clock_fall}ns' D; }} }}"
    )
    for group in ("_po", "_so"):
        if group in present:
            lines.append(
                f"            {_quoted(group)} {{ LHX {{ '0ns' X; "
                f"'{timing.strobe}ns' L/H/X; }} }}"
            )
    lines += ["        }", "    }", "}", ""]
    if chip.scan_ins and chip.scan_outs and chains:
        lines += _scan_structures(chip, chains)
    held = [("_pi", "#" * len(groups.pi))]
    if not chip.scan_ins:
        held.append(("_si", "#" * len(groups.si)))  # the seed
    held += [("_se", "1"), ("_po", "X" * len(groups.po))]
    lines += [
        f"PatternBurst {_quoted(BURST)} {{",
        f"    PatList {{ {_quoted(PATTERN)}; }}",
        "}",
        "",
        "PatternExec {",
        f"    PatternBurst {_quoted(BURST)};",
        "}",
        "",
        "Procedures {",
        f"    {_quoted(PROCEDURE)} {{",
        f"        W {_quoted(WAVEFORM_TABLE)};",
        f"        C {{ {_assign(held)} }}",
    ]
    shifted = [("_si", "#" * len(groups.si))] if chip.scan_ins else []
    shifted += [("_so", "#" * len(groups.so)), ("_clk", "P" * len(groups.clk))]
    lines += [f"        Shift {{ V {{ {_assign(shifted)} }} }}", "    }", "}", ""]
    lines += [f"Pattern {_quoted(PATTERN)} {{", f"    W {_quoted(WAVEFORM_TABLE)};"]
    lines += _Patterns(chip, groups).write(program)
    lines += ["}", ""]
    return "\n".join(lines)


def _scan_structures(chip: Chip, chains: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["ScanStructures {"]
    for chain in sorted(chains, key=lambda c: int(c["index"])):
        index = int(chain["index"])
        cells = " ".join(_quoted(str(cell)) for cell in chain.get("cells", []))
        lines += [
            f"    ScanChain {_quoted(f'chain_{index}')} {{",
            f"        ScanLength {int(chain['length'])};",
            f"        ScanIn {_quoted(chip.scan_ins[index])};",
            f"        ScanOut {_quoted(chip.scan_outs[index])};",
            "        ScanMasterClock "
            + " ".join(_quoted(clock) for clock in chip.clocks)
            + ";",
        ]
        if cells:
            lines.append(f"        ScanCells {cells};")
        lines.append("    }")
    return lines + ["}", ""]


class _Patterns:
    """The Pattern block's statements for a program: a run of one pattern's one
    phase at a time -- a Loop for a preamble, a load_unload call for a load or an
    unload, a vector for a launch or a capture. STIL keeps every signal's last
    waveform character, so the writer keeps what each group holds and a condition
    statement sets only what a run needs changed; after a call, which readers end
    differently, it counts on nothing."""

    def __init__(self, chip: Chip, groups: _Groups) -> None:
        self.chip = chip
        self.groups = groups
        self.held: dict[str, str] = {}
        self.lines: list[str] = []

    def write(self, program: Sequence[Cycle]) -> list[str]:
        labelled: int | None = None
        for (pattern, phase), run in groupby(
            program, key=lambda cycle: (cycle.pattern, cycle.phase)
        ):
            cycles = list(run)
            if any(not cycle.pulse for cycle in cycles):
                raise ScanError(f"STIL: pattern {pattern} has a cycle with no pulse")
            if pattern != labelled:
                self.lines.append(f'    "pattern {pattern}":')
                labelled = pattern
            if phase == "preamble":
                self._preamble(pattern, cycles)
            elif phase in ("load", "unload"):
                self._shifts(pattern, cycles)
            elif phase in ("launch", "capture"):
                for cycle in cycles:
                    self._vector(cycle)
            else:
                raise ScanError(f"STIL: pattern {pattern} has a {phase} phase")
        return self.lines

    def _condition(self, wanted: Sequence[tuple[str, str]]) -> None:
        changes = [(g, data) for g, data in wanted if data and self.held.get(g) != data]
        if changes:
            self.lines.append(f"    C {{ {_assign(changes)} }}")
            self.held.update(changes)

    def _same(self, cycles: Sequence[Cycle], names: Sequence[str]) -> str:
        values = {_drive(cycle, names) for cycle in cycles}
        if len(values) != 1:
            raise ScanError(f"STIL: pattern {cycles[0].pattern}'s inputs change")
        return values.pop()

    def _preamble(self, pattern: int, cycles: list[Cycle]) -> None:
        groups = self.groups
        if any(v is not None for c in cycles for v in c.expect.values()):
            raise ScanError(f"STIL: pattern {pattern}'s preamble compares")
        if self._same(cycles, [groups.se]) != "0":
            raise ScanError(f"STIL: pattern {pattern}'s preamble shifts")
        self._condition(
            [
                ("_pi", self._same(cycles, groups.pi)),
                ("_si", self._same(cycles, groups.si)),
                ("_se", "0"),
                ("_po", "X" * len(groups.po)),
                ("_so", "X" * len(groups.so)),
            ]
        )
        pulse = "P" * len(groups.clk)
        self.lines.append(
            f"    Loop {len(cycles)} {{ V {{ {_assign([('_clk', pulse)])} }} }}"
        )
        self.held["_clk"] = pulse

    def _shifts(self, pattern: int, cycles: list[Cycle]) -> None:
        """A call of the load_unload procedure: the inputs' values (with
        compression, the seed too), and a character per shift for each scan input
        (the load's bits) and each scan output (compared, or X)."""
        chip, groups = self.chip, self.groups
        if self._same(cycles, [groups.se]) != "1":
            raise ScanError(f"STIL: pattern {pattern} shifts with scan enable off")
        data = [("_pi", self._same(cycles, groups.pi))]
        if chip.scan_ins:
            for pin in groups.si:
                data.append((pin, "".join(str(int(c.inputs[pin])) for c in cycles)))
        else:
            data.append(("_si", self._same(cycles, groups.si)))  # the seed
        for pin in groups.so:
            data.append((pin, "".join(_compare(c, [pin]) for c in cycles)))
        self.lines.append(f"    Call {_quoted(PROCEDURE)} {{ {_assign(data)} }}")
        self.held = {}

    def _vector(self, cycle: Cycle) -> None:
        groups = self.groups
        vector = [
            ("_pi", _drive(cycle, groups.pi)),
            ("_si", _drive(cycle, groups.si)),
            ("_se", _drive(cycle, [groups.se])),
            ("_po", _compare(cycle, groups.po)),
            ("_so", _compare(cycle, groups.so)),
            ("_clk", "P" * len(groups.clk)),
        ]
        self.lines.append(f"    V {{ {_assign(vector)} }}")
        self.held.update((group, data) for group, data in vector if data)
