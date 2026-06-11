from __future__ import annotations

from collections.abc import Sequence


def _with_clock(values: dict[str, bool], clock: str, level: bool) -> dict[str, bool]:
    out = dict(values)
    out[clock] = level
    return out


def clock_pulse_cycles(
    values: dict[str, bool],
    clock: str = "CLK",
    sample_after_edge: bool = False,
) -> list[dict[str, bool]]:
    del sample_after_edge
    return [_with_clock(values, clock, False), _with_clock(values, clock, True)]


def scan_shift_cycles(
    bits: list[bool],
    primary_inputs: dict[str, bool],
    clock: str = "CLK",
    scan_in: str = "scan_in",
    scan_enable: str = "scan_en",
) -> list[dict[str, bool]]:
    cycles: list[dict[str, bool]] = []
    for bit in bits:
        values = dict(primary_inputs)
        values[scan_in] = bit
        values[scan_enable] = True
        cycles.extend(clock_pulse_cycles(values, clock))
    return cycles


def scan_capture_cycles(
    primary_inputs: dict[str, bool],
    clock: str = "CLK",
    scan_enable: str = "scan_en",
) -> list[dict[str, bool]]:
    values = dict(primary_inputs)
    values[scan_enable] = False
    return clock_pulse_cycles(values, clock)


def scan_shift_capture_shiftout_cycles(
    shift_in_bits: list[bool],
    capture_inputs: dict[str, bool],
    shift_out_length: int,
    clock: str = "CLK",
    scan_in: str = "scan_in",
    scan_enable: str = "scan_en",
) -> list[dict[str, bool]]:
    cycles = scan_shift_cycles(
        shift_in_bits,
        capture_inputs,
        clock=clock,
        scan_in=scan_in,
        scan_enable=scan_enable,
    )
    cycles.extend(
        scan_capture_cycles(
            capture_inputs,
            clock=clock,
            scan_enable=scan_enable,
        )
    )
    cycles.extend(
        scan_shift_cycles(
            [False] * shift_out_length,
            capture_inputs,
            clock=clock,
            scan_in=scan_in,
            scan_enable=scan_enable,
        )
    )
    return cycles


def _multi_scan_inputs(
    scan_inputs: Sequence[str], values: Sequence[bool], default: bool = False
) -> dict[str, bool]:
    return {
        name: values[index] if index < len(values) else default
        for index, name in enumerate(scan_inputs)
    }


def multi_chain_scan_shift_cycles(
    chains: Sequence[Sequence[bool]],
    primary_inputs: dict[str, bool],
    clock: str = "CLK",
    scan_inputs: Sequence[str] = ("scan_in",),
    scan_enable: str = "scan_en",
) -> list[dict[str, bool]]:
    cycles: list[dict[str, bool]] = []
    depth = max((len(bits) for bits in chains), default=0)
    for offset in range(depth):
        values = dict(primary_inputs)
        values[scan_enable] = True
        values.update(
            _multi_scan_inputs(
                scan_inputs,
                [bits[offset] if offset < len(bits) else False for bits in chains],
            )
        )
        cycles.extend(clock_pulse_cycles(values, clock))
    return cycles


def multi_chain_scan_shift_capture_shiftout_cycles(
    shift_in_bits_by_chain: Sequence[Sequence[bool]],
    capture_inputs: dict[str, bool],
    shift_out_lengths: Sequence[int],
    clock: str = "CLK",
    scan_inputs: Sequence[str] = ("scan_in",),
    scan_enable: str = "scan_en",
) -> list[dict[str, bool]]:
    cycles = multi_chain_scan_shift_cycles(
        shift_in_bits_by_chain,
        capture_inputs,
        clock=clock,
        scan_inputs=scan_inputs,
        scan_enable=scan_enable,
    )
    cycles.extend(
        scan_capture_cycles(capture_inputs, clock=clock, scan_enable=scan_enable)
    )
    cycles.extend(
        multi_chain_scan_shift_cycles(
            [[False] * length for length in shift_out_lengths],
            capture_inputs,
            clock=clock,
            scan_inputs=scan_inputs,
            scan_enable=scan_enable,
        )
    )
    return cycles
