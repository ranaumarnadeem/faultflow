"""Structural verification for the ring-generator + phase-shifter compression
decompressor: re-derives the phase-shifter's XOR structure from the
SYNTHESIZED netlist's own connectivity and diffs it against what the manifest
declares -- the same "structural re-derivation, not SAT-based LEC" discipline
as ``check_scan_structure`` (``faultflow/scan/checks.py``).

SCOPE (deliberately limited, per the compression plan): only the
PHASE-SHIFTER half is checked here. Each ``scan_in_N`` wire's driving cone is
walked back through XOR/XNOR/NOT/BUF gates only, terminating at the declared
``tap_source_net``'s per-bit nets. That net is the ring generator's live
COMBINATIONAL state after the reseed mux (``effective_state`` in
``ring_generator_wrapper_verilog``'s RTL) -- NOT the register ``lfsr_reg``
itself, since the phase shifter reads the post-mux value and
``care_bit_rows`` tracks exactly that value's bit-for-bit trajectory.

Confirmed by direct inspection of real Yosys/abc output on the reference
fixture (do not "fix" this file to trust either of these again without
re-verifying against a real synthesized netlist first):

1. Cell INSTANCE NAMES are Yosys/abc-generated and unstable
   (``$auto$ff.cc:337:slice$93``, ``$abc$130$auto$blifparse.cc:...``) --
   never usable as an identification anchor.
2. **Every real Sky130 library cell's ``port_directions`` dict in this
   project's synthesized JSON is EMPTY** (`{}`) -- confirmed across
   nand2b/mux2/mux2i/xor2/clkinv/edfxtp/dfxtp instances in a real
   ``insert_compression`` run. Only the synthetic, non-Yosys
   ``$scanff_faultflow`` splice cells populate it correctly. This means input
   vs. output pin roles CANNOT be read from ``port_directions`` for any real
   library cell here -- they must come from a hardcoded pin-name table (see
   ``_LINEAR_GATE_PINS`` below), exactly matching how the C++ core already
   never trusts Yosys JSON for cell semantics (CLAUDE.md: "Truth comes from
   the ``gate_type`` + port mapping + ``gate_eval.cpp`` semantics code").
3. NET NAMES the RTL explicitly declares (``effective_state``, ``lfsr_reg``,
   ``tdi``, ``scan_in_0``) DO survive synthesis as ``netnames`` entries,
   which is why this check anchors on those names, not cell instances.
4. A phase-shifter tap list of length 1 (e.g. ``[0]``) gets optimized to a
   pure net alias (no gate at all) -- the walker's leaf check must run
   BEFORE looking for a driving cell, not after.

The ring generator's OWN feedback-tap structure (the register's D-input
cone, which first requires recognizing and stripping the reseed-mux/DFFE-
enable pattern -- real synthesized output for this design mixes
``mux2``/``mux2i``/``nand2b`` cells into that cone) is explicitly NOT checked
here. Building that recognition reliably needs its own empirical spike
against real sky130-synthesized shapes before it can be trusted -- this is
flagged as follow-up work, not silently skipped (see the compression plan).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.scan.errors import ScanError
from faultflow.scan.ring_generator import bitmask_to_index_list
from faultflow.scan.stitch import _load_json, _top_module

# (output_pin, input_pins) for the Sky130 HD gate-type prefixes the walker
# treats as GF(2)-linear (a pure XOR/XNOR/NOT/wire-buffer) -- see
# cells/sky130/sky130_fd_sc_hd.json for the authoritative pin names. Any
# OTHER cell type found driving a net in a phase-shifter cone (a mux, an
# AOI/OAI compound cell, a register) means the netlist doesn't match the
# declared linear decompressor structure.
_LINEAR_GATE_PINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sky130_fd_sc_hd__xor2_": ("X", ("A", "B")),
    "sky130_fd_sc_hd__xnor2_": ("Y", ("A", "B")),
    "sky130_fd_sc_hd__inv_": ("Y", ("A",)),
    "sky130_fd_sc_hd__clkinv_": ("Y", ("A",)),
    "sky130_fd_sc_hd__buf_": ("X", ("A",)),
    "sky130_fd_sc_hd__clkbuf_": ("X", ("A",)),
}
_INVERTING_PREFIXES = (
    "sky130_fd_sc_hd__xnor2_",
    "sky130_fd_sc_hd__inv_",
    "sky130_fd_sc_hd__clkinv_",
)


@dataclass(frozen=True)
class CompressionStructuralCheckResult:
    warnings: list[str]
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def _linear_pins(ctype: str) -> tuple[str, tuple[str, ...]] | None:
    for prefix, spec in _LINEAR_GATE_PINS.items():
        if ctype.startswith(prefix):
            return spec
    return None


def _build_net_index(module: dict[str, Any]) -> dict[int, list[tuple[str, str, dict]]]:
    """net_id -> every cell (instance, type, connections) whose connections
    mention that net on ANY pin. Deliberately does not use
    ``port_directions`` -- confirmed empty for real Sky130 library cells in
    this project's synthesized JSON (see module docstring); pin roles come
    from ``_LINEAR_GATE_PINS``'s hardcoded table instead."""
    index: dict[int, list[tuple[str, str, dict]]] = {}
    for name, cell in module.get("cells", {}).items():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        ctype = str(cell.get("type", ""))
        nets_seen: set[int] = set()
        for nets in conns.values():
            for net in nets:
                if isinstance(net, int):
                    nets_seen.add(net)
        for net in nets_seen:
            index.setdefault(net, []).append((str(name), ctype, conns))
    return index


def _linear_cone(
    net_id: int,
    net_index: dict[int, list[tuple[str, str, dict]]],
    leaf_index: dict[int, int],
    cache: dict[int, tuple[int, bool]],
) -> tuple[int, bool]:
    """(coefficient bitmask over leaf_index's leaves, inverted) for net_id's
    driving cone. Raises ScanError on a non-linear gate, a fan-in count that
    doesn't match the gate's arity, or an undriven net that isn't a declared
    leaf."""
    if net_id in leaf_index:
        return (1 << leaf_index[net_id], False)
    if net_id in cache:
        return cache[net_id]
    for name, ctype, conns in net_index.get(net_id, []):
        spec = _linear_pins(ctype)
        if spec is None:
            continue
        out_pin, in_pins = spec
        if net_id not in conns.get(out_pin, []):
            continue  # this cell touches net_id, but not on its output pin
        input_nets = [
            n for pin in in_pins for n in conns.get(pin, []) if isinstance(n, int)
        ]
        if len(input_nets) != len(in_pins):
            raise ScanError(
                f"{name} ({ctype}): expected {len(in_pins)} input(s), "
                f"found {len(input_nets)}"
            )
        if len(in_pins) == 2:
            a_mask, a_inv = _linear_cone(input_nets[0], net_index, leaf_index, cache)
            b_mask, b_inv = _linear_cone(input_nets[1], net_index, leaf_index, cache)
            mask = a_mask ^ b_mask
            inverted = (a_inv != b_inv) != ctype.startswith("sky130_fd_sc_hd__xnor2_")
        else:
            mask, inv0 = _linear_cone(input_nets[0], net_index, leaf_index, cache)
            inverted = inv0 != ctype.startswith(_INVERTING_PREFIXES)
        cache[net_id] = (mask, inverted)
        return (mask, inverted)
    # No recognized linear gate drives net_id as its declared output. A
    # candidate of an UNRECOGNIZED type touching net_id is presumed to be its
    # (non-linear) driver -- flag it by name. A candidate of a RECOGNIZED
    # linear type that merely reads net_id as one of ITS inputs is not
    # evidence of anything driving net_id -- that means net_id is genuinely
    # undriven (e.g. a dangling/undeclared leaf), not "non-linear".
    for name, ctype, _conns in net_index.get(net_id, []):
        if _linear_pins(ctype) is None:
            raise ScanError(
                f"non-linear gate {ctype!r} (instance {name}) in "
                f"decompressor cone feeding net {net_id}"
            )
    raise ScanError(f"net {net_id} has no driver and is not a declared leaf")


def check_compression_structure(
    manifest: dict[str, Any],
) -> CompressionStructuralCheckResult:
    """Re-derive each ``scan_in`` wire's phase-shifter taps from the
    synthesized, COMPOSED netlist (``manifest["compression"]["composed_json"]``
    -- never ``manifest["generic_json"]``, which must stay pointed at the
    pre-compression, plain scan-stitched netlist so ATPG keeps working; see
    the compression CLI-wiring plan) and require them to match
    ``manifest["compression"]["phase_shifter_taps"]`` exactly.

    A no-op (no errors, no warnings) when compression is absent/disabled in
    the manifest -- this check is opt-in, matching how ``check_scan_structure``
    itself is only meaningful for scan-inserted designs.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        compression = manifest.get("compression")
        if not isinstance(compression, dict) or not compression.get("enabled"):
            return CompressionStructuralCheckResult(warnings=[], errors=[])

        composed_json = Path(str(compression["composed_json"]))
        data = _load_json(composed_json)
        top = str(compression.get("composed_top") or f"{manifest['top']}_compressed")
        _, module = _top_module(data, top)
        netnames = module.get("netnames", {})
        if not isinstance(netnames, dict):
            raise ScanError("module netnames must be an object")

        num_channels = int(compression["num_channels"])
        tap_source_net = str(compression["tap_source_net"])
        scan_in_ports = [str(p) for p in compression["scan_in_ports"]]
        phase_shifter_taps = compression["phase_shifter_taps"]
        if len(scan_in_ports) != len(phase_shifter_taps):
            raise ScanError(
                "manifest compression.scan_in_ports/phase_shifter_taps length "
                "mismatch"
            )

        if tap_source_net not in netnames:
            raise ScanError(
                f"declared tap_source_net {tap_source_net!r} not found in netlist"
            )
        tap_bits = netnames[tap_source_net].get("bits", [])
        if not isinstance(tap_bits, list) or len(tap_bits) != num_channels:
            raise ScanError(
                f"tap_source_net {tap_source_net!r} has "
                f"{len(tap_bits) if isinstance(tap_bits, list) else '?'} bits, "
                f"expected num_channels={num_channels}"
            )
        leaf_index = {int(bit): i for i, bit in enumerate(tap_bits)}

        net_index = _build_net_index(module)
        cache: dict[int, tuple[int, bool]] = {}

        for port, expected_taps in zip(scan_in_ports, phase_shifter_taps):
            if port not in netnames:
                errors.append(f"{port}: scan-in wire not found in netlist")
                continue
            bits = netnames[port].get("bits", [])
            if not isinstance(bits, list) or len(bits) != 1:
                errors.append(f"{port}: scan-in wire must be exactly one bit")
                continue
            try:
                mask, inverted = _linear_cone(
                    int(bits[0]), net_index, leaf_index, cache
                )
            except ScanError as exc:
                errors.append(f"{port}: {exc}")
                continue
            if inverted:
                errors.append(
                    f"{port}: phase-shifter cone is inverted, expected a "
                    "non-inverting XOR tree"
                )
                continue
            expected_mask = 0
            for tap in expected_taps:
                expected_mask ^= 1 << int(tap)
            if mask != expected_mask:
                errors.append(
                    f"{port}: synthesized phase-shifter taps "
                    f"{bitmask_to_index_list(mask)} do not match "
                    f"manifest-declared taps {sorted(int(t) for t in expected_taps)}"
                )
    except (KeyError, ScanError, OSError) as exc:
        errors.append(str(exc))
    return CompressionStructuralCheckResult(warnings=warnings, errors=errors)
