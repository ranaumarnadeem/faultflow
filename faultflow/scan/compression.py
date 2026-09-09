"""Scan test-pattern compression: sequential ring-generator (LFSR) +
phase-shifter decompressor insertion.

Python orchestration layer for the RTL-insertion mechanism described in the
compression plan. The LFSR/phase-shifter *structure* (feedback polynomial,
per-cycle state-transition math) is the single source of truth in
``faultflow.scan.ring_generator`` -- shared by the RTL generator here and by
the GF(2) seed-solving math, so the synthesized hardware and the Python model
used to solve for compressible seeds can never drift out of sync. The
compatibility-checking algorithm itself (``solve_xor_broadcast``, GF(2)
satisfiability of a fixed fanout map against a sparse set of required values)
lives in the C++ core (``src/core/scan/compression.{hpp,cpp}``) and is
unrelated code despite the matching module name -- this file is the
Python-side counterpart, following the same split as e.g.
``faultflow/scan/checks.py`` (Python) vs ``src/core/scan/scan_chain.cpp`` (C++).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.project.assemble import assemble_soc
from faultflow.scan.ring_generator import (
    LfsrPolynomial,
    lookup_polynomial,
    tap_mask_verilog_literal,
)


def _port_decl(name: str, direction: str, width: int) -> str:
    """Match block_stub_verilog's exact port-declaration formatting
    (faultflow/project/assemble.py:105-112) for consistency between the two
    RTL-stub-generation call sites."""
    if width <= 1:
        return f"  {direction} {name};"
    return f"  {direction} [{width - 1}:0] {name};"


def build_broadcast_fanout(
    num_channels: int, num_internal_bits: int
) -> list[list[int]]:
    """Construct a deterministic, full-column-rank XOR phase-shifter map.

    Returns ``fanout``, where ``fanout[i]`` is the sorted list of register-bit
    indices XORed together to drive internal scan-chain input bit ``i``. Every
    register bit appears in at least one internal bit's fanout (no bit is
    structurally dead), and the resulting (num_internal_bits x num_channels)
    matrix has full column rank over GF(2) whenever
    ``num_internal_bits >= num_channels`` (no two register bits are
    indistinguishable from every internal bit's point of view).

    Originally built for a static external-channel broadcast; now reused
    one layer downstream as the ring generator's phase shifter (register bits
    -> chains instead of channels -> chains) -- the full-rank/no-dead-bit
    properties are exactly as desirable either way. This is a deliberately
    simple placeholder construction, not a literature-tuned topology, but is
    grounded in the same one-XOR-per-output decorrelation concept Bardell's
    1990 phase-shifter paper describes (see ring_generator.py's module
    docstring for full citations).
    """
    if num_channels <= 0:
        raise ValueError("num_channels must be positive")
    if num_internal_bits <= 0:
        raise ValueError("num_internal_bits must be positive")

    fanout: list[list[int]] = []
    for i in range(num_internal_bits):
        a = i % num_channels
        b = (i // num_channels) % num_channels
        row = {a, b}
        fanout.append(sorted(row))
    return fanout


def ring_generator_wrapper_verilog(
    core_json: dict[str, Any],
    core_module: str,
    scan_in_ports: list[str],
    polynomial: LfsrPolynomial,
    phase_shifter_taps: list[list[int]],
    wrapper_module: str,
    channel_port: str = "tdi",
    scan_enable_port: str = "scan_en",
    clock_port: str = "clk",
    instance_name: str = "core_inst",
) -> str:
    """Emit sequential decompressor wrapper RTL: instantiate ``core_module`` by
    name (a real instantiation -- blackboxing happens later, in assemble.py's
    block_stub_verilog, not here) with every port wired straight through
    except ``scan_in_ports``, which are driven by a static XOR phase-shifter
    reading a K-bit ring-generator register (``lfsr_reg``). The register is
    parallel-loaded from an external ``channel_port`` (a
    ``polynomial.width``-wide bus) on a self-timed one-shot reseed pulse
    (``reseed = scan_enable_port & ~prev_scan_en``, firing on the first
    ``scan_en=1`` cycle after an idle cycle -- i.e. automatically at the start
    of each pattern's load phase, no new external control signal needed), then
    free-runs under its own feedback taps (``polynomial.taps``, rendered via
    ``tap_mask_verilog_literal`` -- the RTL generator never hand-writes tap
    bits, so it can't silently drift from the polynomial table) on every
    subsequent ``scan_en=1`` shift clock.

    KNOWN, DELIBERATE SUBTLETY: ``scan_enable_port`` also rises 0->1 at the
    start of a pattern's *unload* phase (after the SE=0 capture window),
    firing a second, harmless reseed pulse. This is correctness-neutral only
    because (a) unload-phase garbage injected at a chain's head cannot reach
    scan_out within the same max_chain_length-cycle unload window, and (b)
    every pattern is simulated as an independent call with fresh FF state
    (confirmed against src/core/sim/state/sim_state.hpp -- every FF defaults
    to 0 unless TestVector.initial_ff_state overrides it, which
    build_scan_pattern_vector never does for these registers). A future change
    that merges load+unload across patterns into one continuous shift stream,
    or carries FF state between simulate_scan_pattern calls, would break this
    invariant silently -- re-check this comment before making that change.

    ``scan_in_ports`` is the ordered list of the design's actual scan-chain
    input port names (one per chain -- faultflow.scan.stitch.scan_port_names()
    returns e.g. ``["scan_in_0", "scan_in_1"]`` for a 2-chain design, never a
    single N-bit bus), and ``phase_shifter_taps`` must have exactly one row per
    entry, matching build_broadcast_fanout's return shape when called with
    ``num_internal_bits=len(scan_in_ports)``.

    ``scan_enable_port``/``clock_port`` must already be real ports of
    ``core_module`` (i.e. present in ``passthrough_names``, not new top-level
    ports of the wrapper) -- the ring generator's clock is tied to the
    design's *existing* clock net, never a new port, so no new clock domain is
    ever introduced (see the compression plan's rule_check discussion).

    Pure string templating -- no Yosys involved in generating this text; Yosys
    only sees it later as an input file to synthesize.

    ``instance_name`` MUST match whatever key the caller uses for this core in
    assemble_soc()'s ``blocks``/``block_module`` dicts -- compose_soc() finds
    the block's blackboxed instance cell by looking up
    ``result_module["cells"][inst]`` where ``inst`` is that same dict key
    (faultflow/project/assemble.py:165-169), so the instantiation name emitted
    here and the ``blocks`` key passed to assemble_soc() are the same string
    by construction, not by coincidence -- insert_compression() below always
    passes the same value to both.
    """
    if len(phase_shifter_taps) != len(scan_in_ports):
        raise ValueError(
            f"fanout has {len(phase_shifter_taps)} rows but there are "
            f"{len(scan_in_ports)} scan_in_ports -- exactly one fanout row "
            "per scan chain is required"
        )
    modules = core_json.get("modules")
    if not isinstance(modules, dict) or core_module not in modules:
        raise ValueError(f"core JSON has no module {core_module!r}")
    ports = modules[core_module].get("ports", {})
    if not isinstance(ports, dict):
        raise ValueError(f"module {core_module!r} has no ports")

    scan_in_set = set(scan_in_ports)
    passthrough_names = [name for name in ports if name not in scan_in_set]

    if scan_enable_port not in passthrough_names:
        raise ValueError(
            f"scan_enable_port {scan_enable_port!r} is not a port of "
            f"{core_module!r} (or is itself one of scan_in_ports)"
        )
    if clock_port not in passthrough_names:
        raise ValueError(
            f"clock_port {clock_port!r} is not a port of {core_module!r} "
            "(or is itself one of scan_in_ports)"
        )

    width = polynomial.width

    port_decls: list[str] = []
    port_list: list[str] = passthrough_names + [channel_port]
    for name in passthrough_names:
        port = ports[name]
        direction = port.get("direction", "input")
        port_width = len(port.get("bits", []))
        port_decls.append(_port_decl(name, direction, port_width))
    port_decls.append(_port_decl(channel_port, "input", width))

    reg_width = f"[{width - 1}:0] " if width > 1 else ""
    wire_decls = [f"  wire {name};" for name in scan_in_ports]
    assigns = []
    for name, taps in zip(scan_in_ports, phase_shifter_taps):
        terms = " ^ ".join(f"effective_state[{t}]" for t in taps)
        assigns.append(f"  assign {name} = {terms};")

    inst_conns = [
        f"    .{name}({name})" for name in passthrough_names + scan_in_ports
    ]

    lines = [
        f"module {wrapper_module}({', '.join(port_list)});",
        *port_decls,
        "",
        *wire_decls,
        "",
        f"  localparam {reg_width}TAP_MASK = {tap_mask_verilog_literal(polynomial)};",
        "",
        f"  reg {reg_width}lfsr_reg;",
        "  reg prev_scan_en;",
        f"  wire reseed = {scan_enable_port} & ~prev_scan_en;",
        f"  wire {reg_width}effective_state = reseed ? {channel_port} : lfsr_reg;",
        f"  wire fb = effective_state[{width - 1}];",
        f"  wire {reg_width}next_state;",
        "  assign next_state[0] = fb;",
        "  genvar i;",
        "  generate",
        f"    for (i = 1; i < {width}; i = i + 1) begin : lfsr_tap",
        "      assign next_state[i] = effective_state[i-1] ^ (TAP_MASK[i] ? fb : 1'b0);",
        "    end",
        "  endgenerate",
        "",
        f"  always @(posedge {clock_port}) begin",
        f"    prev_scan_en <= {scan_enable_port};",
        f"    if ({scan_enable_port})",
        "      lfsr_reg <= next_state;",
        "  end",
        "",
        *assigns,
        "",
        f"  {core_module} {instance_name} (",
        ",\n".join(inst_conns),
        "  );",
        "endmodule",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class CompressionMap:
    """The structure a caller needs to persist into a manifest and to solve
    for compressible seeds (via ring_generator.py::care_bit_rows +
    solve_xor_broadcast) after ``insert_compression`` runs."""

    num_channels: int
    polynomial: LfsrPolynomial
    phase_shifter_taps: list[list[int]]


def insert_compression(
    core_json_path: Path,
    core_top: str,
    scan_in_ports: list[str],
    num_channels: int,
    liberty: Path,
    output_json: Path,
    *,
    workdir: Path,
    clock_port: str = "clk",
    scan_enable_port: str = "scan_en",
) -> tuple[Path, CompressionMap]:
    """Insert a sequential ring-generator + phase-shifter decompressor around
    a frozen, already scan-stitched core netlist, producing one flat composed
    netlist at ``output_json``.

    Reuses faultflow.project.assemble.assemble_soc() UNCHANGED: compression
    insertion is a degenerate one-block composition, where the core design
    plays the role of the single "block" and the newly-generated ring
    generator wrapper RTL plays the role of "SoC glue" -- assemble_soc already
    handles blackboxing the frozen core (via block_stub_verilog, so Yosys's
    optimizer never touches it) and splicing its real cells back in afterward
    (compose_soc). See the compression plan for the full reuse rationale.

    ``num_channels`` must be a curated LFSR width (see
    ``ring_generator.lookup_polynomial`` -- raises ``ValueError`` otherwise).
    ``scan_in_ports`` must be sourced by the caller (typically from the
    design's scan manifest) -- deriving it automatically is a later increment,
    not part of this one.

    Returns the composed netlist path (== ``output_json``) and a
    ``CompressionMap`` describing the decompressor's structure, which the
    caller needs for ATPG integration (seed-solving) and for persisting into a
    manifest.
    """
    core_json = json.loads(core_json_path.read_text(encoding="utf-8"))
    polynomial = lookup_polynomial(num_channels)
    phase_shifter_taps = build_broadcast_fanout(num_channels, len(scan_in_ports))
    wrapper_top = f"{core_top}_compressed"
    instance_name = "core_inst"
    wrapper_rtl = ring_generator_wrapper_verilog(
        core_json,
        core_top,
        scan_in_ports,
        polynomial,
        phase_shifter_taps,
        wrapper_top,
        clock_port=clock_port,
        scan_enable_port=scan_enable_port,
        instance_name=instance_name,
    )

    workdir.mkdir(parents=True, exist_ok=True)
    wrapper_path = workdir / f"{wrapper_top}.v"
    wrapper_path.write_text(wrapper_rtl, encoding="utf-8")

    assemble_soc(
        soc_rtl=wrapper_path,
        soc_top=wrapper_top,
        liberty=liberty,
        blocks={instance_name: core_json_path},
        block_module={instance_name: core_top},
        output_json=output_json,
        workdir=workdir,
    )
    return output_json, CompressionMap(num_channels, polynomial, phase_shifter_taps)
