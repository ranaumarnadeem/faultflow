from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from faultflow.scan.atpg_view import PPI_PREFIX, PPO_PREFIX
from faultflow.scan.protocol_constants import load_sequence, unload_sequence


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


@dataclass(frozen=True)
class ScanPattern:
    load_seqs: dict[int, list[bool]]
    capture_pi_values: dict[str, bool]
    expected_unload: dict[int, list[bool]]


def _chain_lengths(manifest: dict[str, Any]) -> dict[int, int]:
    chains = manifest.get("chains", [])
    if not isinstance(chains, list):
        return {}
    lengths: dict[int, int] = {}
    for raw in chains:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        length = raw.get("length")
        if isinstance(index, int) and isinstance(length, int):
            lengths[index] = length
    return lengths


def serialize_vector(
    vector: dict[str, bool],
    pseudo_port_map: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> ScanPattern:
    lengths = _chain_lengths(manifest)
    load_by_chain: dict[int, dict[int, bool]] = {}
    unload_by_chain: dict[int, dict[int, bool]] = {}
    for entry in pseudo_port_map.values():
        chain_id = int(entry["chain_id"])
        position = int(entry["position_in_chain"])
        load_by_chain.setdefault(chain_id, {})[position] = bool(
            vector.get(str(entry["ppi_port"]), False)
        )
        unload_by_chain.setdefault(chain_id, {})[position] = bool(
            vector.get(str(entry["ppo_port"]), False)
        )
    load_seqs = {
        chain_id: load_sequence(targets, lengths.get(chain_id, len(targets)))
        for chain_id, targets in load_by_chain.items()
    }
    expected_unload = {
        chain_id: unload_sequence(targets, lengths.get(chain_id, len(targets)))
        for chain_id, targets in unload_by_chain.items()
    }
    capture_pi_values = {
        name: bool(value)
        for name, value in vector.items()
        if not name.startswith(PPI_PREFIX) and not name.startswith(PPO_PREFIX)
    }
    return ScanPattern(
        load_seqs=load_seqs,
        capture_pi_values=capture_pi_values,
        expected_unload=expected_unload,
    )
