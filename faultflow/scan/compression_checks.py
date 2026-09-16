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

The ring generator's OWN feedback-tap/reseed-mux cone (the register's
D-input logic, ``next_state``) IS also checked here, as of a later empirical
spike against real sky130-synthesized shapes (two curated widths, 8 and 16,
both confirmed). Findings from that spike, load-bearing for the design below
-- do not "fix" this file to trust anything about this cone again without
re-verifying against a real synthesized netlist first:

5. ``reseed`` (``wire reseed = scan_en & ~prev_scan_en;`` in the RTL) does
   NOT survive synthesis as a named net, unlike ``lfsr_reg``/``tdi``/
   ``effective_state``/``next_state``/``prev_scan_en``. ABC instead computes
   its COMPLEMENT directly: a ``nand2b(A_N=prev_scan_en, B=scan_en)`` cell,
   whose output (De Morgan: ``prev_scan_en | ~scan_en`` == ``~reseed``) is
   shared as the ``S`` operand by every reseed-mux in the cone. ``prev_scan_en``
   itself is a plain, un-enabled ``dfxtp`` fed directly by the scan-enable
   port (matching the RTL's unconditional ``prev_scan_en <= scan_en;``).
6. Most ``effective_state`` bits get one clean ``mux2``/``mux2i`` cell
   (``A0``=reseed-active branch, ``A1``=natural-feedback branch, given this
   design's specific ``S == ~reseed`` polarity -- ``mux2``: non-inverting,
   ``X = S ? A1 : A0``; ``mux2i``: inverting, ``Y = ~(S ? A1 : A0)``). The
   widest-fanout bit (the feedback bit ``fb``, i.e. ``effective_state[width-1]``)
   instead gets ``mux2i`` + a separate ``clkinv`` (ABC's fanout-driven choice,
   functionally equivalent, two gates not one).
7. For every TAP bit (``i in polynomial.taps``), ABC exploits
   ``XOR(~A,~B) == XOR(A,B)``: it computes BOTH XOR operands in already-
   INVERTED form via ``mux2i`` (reusing the already-inverted ``fb``) and feeds
   them straight into ``xor2`` -- the plain, non-inverted
   ``effective_state[i-1]`` is never built as its own net at all. Its
   ``netnames`` entry is a real but GHOST name: listed, but undriven by any
   cell in the actual optimized netlist. Confirmed empirically: the set of
   ghost ``effective_state`` bits is always exactly ``{t - 1 for t in
   polynomial.taps}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faultflow.scan.errors import ScanError
from faultflow.scan.ring_generator import bitmask_to_index_list, lookup_polynomial
from faultflow.scan.stitch import _load_json, _top_module

# Fixed RTL-declared net names from ring_generator_wrapper_verilog -- never
# parameterized in that function, so hardcoding them here mirrors how
# "effective_state" (manifest key tap_source_net) is itself always a hardcoded
# constant in Runner.scan_compress(), just accessed via a level of manifest
# indirection that these three don't need.
_LFSR_REG_NET_NAME = "lfsr_reg"
_NEXT_STATE_NET_NAME = "next_state"
_PREV_SCAN_EN_NET_NAME = "prev_scan_en"
_TDI_NET_NAME = "tdi"

# (output_pin, (A0, A1, S) pin names, is_inverting) for the Sky130 mux
# variants used as the reseed-select mux, per finding 6 above.
_MUX_GATE_PINS: dict[str, tuple[str, tuple[str, str, str], bool]] = {
    "sky130_fd_sc_hd__mux2_": ("X", ("A0", "A1", "S"), False),
    "sky130_fd_sc_hd__mux2i_": ("Y", ("A0", "A1", "S"), True),
}

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
    mux_leaf_resolver: Callable[[int], tuple[int, bool] | None] | None = None,
) -> tuple[int, bool]:
    """(coefficient bitmask over leaf_index's leaves, inverted) for net_id's
    driving cone. Raises ScanError on a non-linear gate, a fan-in count that
    doesn't match the gate's arity, or an undriven net that isn't a declared
    leaf.

    ``mux_leaf_resolver``, when given, is tried as a fallback leaf case right
    before giving up on net_id: used by the register feedback-tap cone (see
    ``_reseed_mux_resolver``) to recognize a reseed-mux-driven net as an
    ``effective_state[k]`` leaf, without teaching this function anything
    about mux2/mux2i (MUX is not itself a linear GF(2) operation, unlike
    every gate in ``_LINEAR_GATE_PINS``) -- disabled (``None``) for the
    phase-shifter's own calls, which is unaffected either way.
    """
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
            a_mask, a_inv = _linear_cone(
                input_nets[0], net_index, leaf_index, cache, mux_leaf_resolver
            )
            b_mask, b_inv = _linear_cone(
                input_nets[1], net_index, leaf_index, cache, mux_leaf_resolver
            )
            mask = a_mask ^ b_mask
            inverted = (a_inv != b_inv) != ctype.startswith("sky130_fd_sc_hd__xnor2_")
        else:
            mask, inv0 = _linear_cone(
                input_nets[0], net_index, leaf_index, cache, mux_leaf_resolver
            )
            inverted = inv0 != ctype.startswith(_INVERTING_PREFIXES)
        cache[net_id] = (mask, inverted)
        return (mask, inverted)
    if mux_leaf_resolver is not None:
        resolved = mux_leaf_resolver(net_id)
        if resolved is not None:
            cache[net_id] = resolved
            return resolved
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


def _find_reseed_select_net(
    cells: dict[str, Any], prev_scan_en_net: int, scan_enable_net: int
) -> int:
    """Find the nand2b-family cell computing ``prev_scan_en | ~scan_en`` (De
    Morgan's complement of ``reseed = scan_en & ~prev_scan_en``) and return
    its output net. Real Yosys/abc synthesis does not preserve ``reseed`` as
    a named net (finding 5) -- it computes the complement directly, and
    every reseed-mux in the cone shares this one net as its select input."""
    for name, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        ctype = str(cell.get("type", ""))
        if not ctype.startswith("sky130_fd_sc_hd__nand2b_"):
            continue
        conns = cell.get("connections", {})
        a_n = conns.get("A_N", [])
        b = conns.get("B", [])
        y = conns.get("Y", [])
        if (
            len(a_n) == 1
            and int(a_n[0]) == prev_scan_en_net
            and len(b) == 1
            and int(b[0]) == scan_enable_net
            and len(y) == 1
        ):
            return int(y[0])
    raise ScanError(
        "could not find the reseed-select nand2b(prev_scan_en, scan_enable) cell"
    )


def _verify_prev_scan_en_register(
    cells: dict[str, Any], prev_scan_en_net: int, scan_enable_net: int
) -> None:
    """prev_scan_en must be a plain, un-enabled dfxtp-family FF fed directly
    by the scan-enable port -- matching the RTL's unconditional
    ``prev_scan_en <= scan_en;`` (no ``if`` guard, unlike ``lfsr_reg``)."""
    for name, cell in cells.items():
        if not isinstance(cell, dict):
            continue
        ctype = str(cell.get("type", ""))
        if not ctype.startswith("sky130_fd_sc_hd__dfxtp_"):
            continue
        conns = cell.get("connections", {})
        q = conns.get("Q", [])
        if len(q) == 1 and int(q[0]) == prev_scan_en_net:
            d = conns.get("D", [])
            if len(d) == 1 and int(d[0]) == scan_enable_net:
                return
            raise ScanError(
                f"{name} ({ctype}): prev_scan_en register's D input is not "
                "directly the scan-enable port"
            )
    raise ScanError("could not find prev_scan_en's own driving register")


def _reseed_mux_resolver(
    net_index: dict[int, list[tuple[str, str, dict]]],
    tdi_index: dict[int, int],
    lfsr_reg_index: dict[int, int],
    reseed_select_net: int,
) -> Callable[[int], tuple[int, bool] | None]:
    """Leaf resolver for ``_linear_cone``: recognizes a net driven by a
    mux2/mux2i cell whose S operand is ``reseed_select_net`` (== ``~reseed``,
    per this design's derived polarity -- finding 5) and whose A0/A1 operands
    are ``tdi[k]``/``lfsr_reg[k]`` for a CONSISTENT k (A0 = the reseed-active
    branch given S == ~reseed, A1 = the natural-feedback branch), returning
    ``(1 << k, mux_is_inverting)`` -- the same leaf shape ``_linear_cone``
    already uses for any other leaf, since a correctly reseed-selected net IS
    leaf k (``effective_state[k]``) for the outer feedback-tap cone walk.

    Raises ScanError (not None) for a mux2/mux2i cell whose S matches but
    whose A0/A1 wiring doesn't resolve to a consistent tdi/lfsr_reg pair --
    that is a real defect, not "this isn't a reseed mux"."""

    def resolve(net_id: int) -> tuple[int, bool] | None:
        for name, ctype, conns in net_index.get(net_id, []):
            spec = None
            for prefix, mux_spec in _MUX_GATE_PINS.items():
                if ctype.startswith(prefix):
                    spec = mux_spec
                    break
            if spec is None:
                continue
            out_pin, (a0_pin, a1_pin, s_pin) = spec[0], spec[1]
            inverting = spec[2]
            if net_id not in conns.get(out_pin, []):
                continue
            s_nets = conns.get(s_pin, [])
            if len(s_nets) != 1 or int(s_nets[0]) != reseed_select_net:
                continue
            a0_nets = conns.get(a0_pin, [])
            a1_nets = conns.get(a1_pin, [])
            if len(a0_nets) != 1 or len(a1_nets) != 1:
                raise ScanError(
                    f"{name} ({ctype}): expected exactly one net on each of "
                    f"{a0_pin}/{a1_pin}"
                )
            a0_net, a1_net = int(a0_nets[0]), int(a1_nets[0])
            a0_k = tdi_index.get(a0_net)
            a1_k = lfsr_reg_index.get(a1_net)
            if a0_k is None or a1_k is None or a0_k != a1_k:
                raise ScanError(
                    f"{name} ({ctype}): reseed mux operands do not form a "
                    f"consistent tdi[k]/lfsr_reg[k] pair (A0 net {a0_net} -> "
                    f"tdi index {a0_k}, A1 net {a1_net} -> lfsr_reg index "
                    f"{a1_k})"
                )
            return (1 << a0_k, inverting)
        return None

    return resolve


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

        # --- the ring generator's OWN feedback-tap/reseed-mux cone: set up
        # FIRST, not after the phase-shifter loop below. A "ghost" effective_
        # state bit (finding 7 -- e.g. i-1 for i in polynomial.taps) is only
        # reachable via the SAME reseed-mux-driven net the register cone
        # uses; if the phase-shifter's OWN fan-out happens to XOR a ghost
        # bit (confirmed empirically: happens with >2 real scan chains,
        # where build_broadcast_fanout's tap assignment can land on a ghost
        # index), its _linear_cone walk needs mux_resolver too, or it
        # wrongly reports "non-linear gate" for a perfectly valid netlist.
        polynomial = lookup_polynomial(num_channels)
        cells = module.get("cells", {})
        if not isinstance(cells, dict):
            raise ScanError("module cells must be an object")

        for required in (
            _LFSR_REG_NET_NAME,
            _NEXT_STATE_NET_NAME,
            _PREV_SCAN_EN_NET_NAME,
            _TDI_NET_NAME,
        ):
            if required not in netnames:
                raise ScanError(f"declared {required!r} net not found in netlist")
        lfsr_reg_bits = netnames[_LFSR_REG_NET_NAME].get("bits", [])
        next_state_bits = netnames[_NEXT_STATE_NET_NAME].get("bits", [])
        tdi_bits = netnames[_TDI_NET_NAME].get("bits", [])
        prev_scan_en_bits = netnames[_PREV_SCAN_EN_NET_NAME].get("bits", [])
        if not (
            isinstance(lfsr_reg_bits, list)
            and len(lfsr_reg_bits) == num_channels
            and isinstance(next_state_bits, list)
            and len(next_state_bits) == num_channels
            and isinstance(tdi_bits, list)
            and len(tdi_bits) == num_channels
        ):
            raise ScanError(
                f"{_LFSR_REG_NET_NAME!r}/{_NEXT_STATE_NET_NAME!r}/"
                f"{_TDI_NET_NAME!r} net widths do not all match "
                f"num_channels={num_channels}"
            )
        if not isinstance(prev_scan_en_bits, list) or len(prev_scan_en_bits) != 1:
            raise ScanError(f"{_PREV_SCAN_EN_NET_NAME!r} must be exactly one bit")

        scan_enable_port = str(compression["scan_enable_port"])
        if scan_enable_port not in netnames:
            raise ScanError(
                f"scan_enable_port {scan_enable_port!r} not found in netlist"
            )
        scan_enable_bits = netnames[scan_enable_port].get("bits", [])
        if not isinstance(scan_enable_bits, list) or len(scan_enable_bits) != 1:
            raise ScanError(
                f"scan_enable_port {scan_enable_port!r} must be exactly one bit"
            )

        reseed_select_net = _find_reseed_select_net(
            cells, int(prev_scan_en_bits[0]), int(scan_enable_bits[0])
        )
        _verify_prev_scan_en_register(
            cells, int(prev_scan_en_bits[0]), int(scan_enable_bits[0])
        )

        tdi_index = {int(bit): i for i, bit in enumerate(tdi_bits)}
        lfsr_reg_index = {int(bit): i for i, bit in enumerate(lfsr_reg_bits)}
        mux_resolver = _reseed_mux_resolver(
            net_index, tdi_index, lfsr_reg_index, reseed_select_net
        )

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
                    int(bits[0]), net_index, leaf_index, cache, mux_resolver
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

        # Verify each effective_state[k]'s OWN construction (the reseed mux
        # itself), for every bit that has a real, directly-named driver.
        # This must NOT reuse `leaf_index` (which maps these same nets to
        # themselves as trivial leaves for the phase-shifter/tap-formula
        # walks below) -- doing so would short-circuit before ever reaching
        # the mux, silently accepting ANY wiring at all for these bits
        # (confirmed empirically: swapping a "clean" bit's mux A0/A1 went
        # undetected before this loop was added). An EMPTY leaf_index here
        # forces every bit through mux_resolver. GHOST bits (finding 7 --
        # undriven, e.g. i-1 for i in polynomial.taps) have no entry in
        # net_index at all and are skipped here, not silently: their reseed
        # correctness is still verified below, indirectly, via the
        # tap-formula loop, since that's the only place their value is ever
        # actually consumed in the real netlist.
        for k, bit in enumerate(tap_bits):
            net_id = int(bit)
            if net_id not in net_index:
                continue  # ghost bit -- verified indirectly via tap formula
            try:
                mask, inverted = _linear_cone(net_id, net_index, {}, {}, mux_resolver)
            except ScanError as exc:
                errors.append(f"{tap_source_net}[{k}]: {exc}")
                continue
            if inverted or mask != (1 << k):
                errors.append(
                    f"{tap_source_net}[{k}]: does not resolve to a "
                    f"non-inverted reseed-mux select of tdi[{k}]/"
                    f"lfsr_reg[{k}] (got mask="
                    f"{bitmask_to_index_list(mask)}, inverted={inverted})"
                )

        # effective_state[k] (leaf_index, already built above for the
        # phase-shifter) is leaf k for this walk too -- the SAME live value,
        # reused exactly as the real design shares it. A separate cache: no
        # net actually overlaps between the two cones (disjoint fan-out from
        # effective_state), but a fresh cache keeps that provable rather than
        # assumed.
        register_cache: dict[int, tuple[int, bool]] = {}

        for i in range(num_channels):
            try:
                mask, inverted = _linear_cone(
                    int(next_state_bits[i]),
                    net_index,
                    leaf_index,
                    register_cache,
                    mux_resolver,
                )
            except ScanError as exc:
                errors.append(f"next_state[{i}]: {exc}")
                continue
            if inverted:
                errors.append(
                    f"next_state[{i}]: feedback cone is inverted, expected a "
                    "non-inverting XOR"
                )
                continue
            fb_mask = 1 << (num_channels - 1)
            if i == 0:
                expected_mask = fb_mask
            else:
                expected_mask = (1 << (i - 1)) ^ (
                    fb_mask if i in polynomial.taps else 0
                )
            if mask != expected_mask:
                errors.append(
                    f"next_state[{i}]: synthesized feedback cone "
                    f"{bitmask_to_index_list(mask)} does not match expected "
                    f"{bitmask_to_index_list(expected_mask)} (polynomial "
                    f"taps={sorted(polynomial.taps)})"
                )
    except (KeyError, ScanError, OSError) as exc:
        errors.append(str(exc))
    return CompressionStructuralCheckResult(warnings=warnings, errors=errors)
