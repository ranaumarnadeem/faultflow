from __future__ import annotations

from pathlib import Path
from typing import Any

from faultflow.config import FaultflowConfig
from faultflow.scan.atpg_view import PPO_PREFIX
from faultflow.scan.cell_map import resolve_scan_cell_map
from faultflow.scan.protocol import ScanPattern, serialize_vector


def verify_scan_pattern_tier_b(
    cfg: FaultflowConfig,
    manifest: dict[str, Any],
    generic_json: Path,
    pattern: ScanPattern,
    *,
    vector_index: int,
    fault_id: int | None,
    reduced_vector: dict[str, bool],
    functional_output_order: list[str],
) -> None:
    from faultflow.runner.runner import RunnerError, _load_core, _port_name_for_net

    core = _load_core()
    if core is None:
        raise RunnerError(
            "C++ extension _faultflow_core is required. "
            "Run: cmake --build build -- -j2"
        )

    clock_net = manifest.get("clock_net")
    if not isinstance(clock_net, int):
        raise RunnerError("scan manifest clock_net must be an integer")
    clock_port = _port_name_for_net(
        generic_json, str(manifest["top"]), clock_net, "input"
    )
    if clock_port is None:
        raise RunnerError(f"cannot map scan clock net {clock_net} to a port")

    scan_inputs = manifest.get("scan_inputs", [])
    scan_outputs = manifest.get("scan_outputs", [])
    if not isinstance(scan_inputs, list) or not isinstance(scan_outputs, list):
        raise RunnerError("manifest scan_inputs/scan_outputs must be lists")
    scan_input_ports = [str(name) for name in scan_inputs]
    scan_output_ports = [str(name) for name in scan_outputs]
    scan_enable = str(manifest["scan_enable"])
    max_chain_length = int(manifest.get("max_chain_length", 0))

    cell_map = resolve_scan_cell_map(cfg)
    result = dict(
        core.simulate_scan_pattern(
            str(generic_json),
            str(cell_map),
            clock_port,
            scan_enable,
            scan_input_ports,
            scan_output_ports,
            functional_output_order,
            max_chain_length,
            pattern.load_seqs,
            pattern.capture_pi_values,
            cfg.simulation.unsupported_cells,
        )
    )
    real_po_values = dict(result.get("real_po_values", {}))
    unload_raw = result.get("unload_seqs", {})
    unload_seqs = {
        int(chain_id): [bool(bit) for bit in bits]
        for chain_id, bits in dict(unload_raw).items()
    }

    for port in functional_output_order:
        expected = bool(reduced_vector.get(port, False))
        actual = bool(real_po_values.get(port, False))
        if expected != actual:
            raise RunnerError(
                "Tier B scan protocol mismatch on functional PO "
                f"vector_index={vector_index} fault_id={fault_id} "
                f"port={port} expected={expected} actual={actual}"
            )

    for chain_id, expected_bits in pattern.expected_unload.items():
        actual_bits = unload_seqs.get(chain_id, [])
        if actual_bits != expected_bits:
            raise RunnerError(
                "Tier B scan protocol mismatch on unload sequence "
                f"vector_index={vector_index} fault_id={fault_id} "
                f"chain_id={chain_id} expected={expected_bits} actual={actual_bits}"
            )


def make_tier_b_verifier(
    cfg: FaultflowConfig,
    manifest: dict[str, Any],
    generic_json: Path,
    pseudo_port_map: dict[str, dict[str, Any]],
    reduced_output_order: list[str],
):
    functional_output_order = [
        port for port in reduced_output_order if not port.startswith(PPO_PREFIX)
    ]
    state = {"vector_index": 0}

    def _verify(vector: dict[str, bool], fault_id: int | None) -> None:
        state["vector_index"] += 1
        pattern = serialize_vector(vector, pseudo_port_map, manifest)
        verify_scan_pattern_tier_b(
            cfg,
            manifest,
            generic_json,
            pattern,
            vector_index=state["vector_index"],
            fault_id=fault_id,
            reduced_vector=vector,
            functional_output_order=functional_output_order,
        )

    return _verify
