"""SoC golden gate: prove a retargeted pattern reproduces the block's response.

Replays the retargeted SoC-level load/capture/unload on the assembly netlist through
the SAME scan-protocol simulator the block used, then compares ONLY the SoC unload
positions the block owns (``unload_mask``). Sibling / BYPASS positions are masked
because, during one block's INTEST, the other blocks are inert and emit don't-care
bits — a raw full-chain compare would spuriously fail on them. A pass means the
block's coverage transfers to chip pins with no re-ATPG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.retarget.soc_access import SocAccess
from faultflow.retarget.transform import SocScanPattern


@dataclass(frozen=True)
class SocVerifyResult:
    verified: bool
    compared: int  # number of block-owned unload bits checked
    mismatches: list[tuple[int, int, bool, bool | None]] = field(default_factory=list)


def verify_soc(
    soc_netlist: str | Path,
    cell_map: str | Path,
    soc_access: SocAccess,
    pattern: SocScanPattern,
    *,
    unsupported_policy: str = "blackbox",
    test_mode: str = "intest",
    core: Any | None = None,
) -> SocVerifyResult:
    """Replay `pattern` on `soc_netlist` and compare the masked (block-owned) unload."""
    if core is None:
        from faultflow.runner.runner import _load_core

        core = _load_core()
    if core is None:  # pragma: no cover - exercised only without the built core
        raise RuntimeError("faultflow C++ core is not available")

    chains = sorted(soc_access.soc_chains, key=lambda c: c.index)
    scan_in = [c.scan_in for c in chains]
    scan_out = [c.scan_out for c in chains]
    result = core.simulate_scan_pattern(
        json_path=str(soc_netlist),
        cell_map_path=str(cell_map),
        clock_ports=list(soc_access.clock_ports),
        clock_off_states=[False] * len(soc_access.clock_ports),
        scan_enable_port=soc_access.scan_enable,
        scan_input_ports=scan_in,
        scan_output_ports=scan_out,
        functional_output_ports=[],
        max_chain_length=soc_access.max_chain_length,
        load_seqs=pattern.load_seqs,
        capture_pi_values=pattern.capture_pi_values,
        unsupported_policy=unsupported_policy,
        test_mode=test_mode,
    )
    unload = result["unload_seqs"]

    compared = 0
    mismatches: list[tuple[int, int, bool, bool | None]] = []
    for chain in chains:
        got = list(unload.get(chain.index, []))
        exp = pattern.expected_unload.get(chain.index, [])
        mask = pattern.unload_mask.get(chain.index, [])
        for k, owned in enumerate(mask):
            if not owned:
                continue
            compared += 1
            actual = bool(got[k]) if k < len(got) else None
            if actual is None or actual != bool(exp[k]):
                mismatches.append((chain.index, k, bool(exp[k]), actual))
    return SocVerifyResult(
        verified=not mismatches, compared=compared, mismatches=mismatches
    )
