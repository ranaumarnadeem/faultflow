from __future__ import annotations


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
    scan_in: str = "scan_in_0",
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
    scan_in: str = "scan_in_0",
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
