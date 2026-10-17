"""Scan test-pattern compression: the continuous-flow decompressor, a ring
generator (LFSR) that takes the channel bits on every shift, and a phase shifter.

Python orchestration layer for the RTL-insertion mechanism. The ring's structure
(feedback polynomial, per-shift state transition) is the single source of truth in
``faultflow.scan.ring_generator`` -- shared by the RTL generator here, the checks
of the synthesized netlist (``decompressor_check``) and the load-as-a-program the
SAT solves take (``ring_generator.continuous_seed_program``) -- so the hardware and
the models used to solve for compressible loads can never drift apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.pdk.synth import LibraryArg
from faultflow.project.assemble import assemble_soc
from faultflow.scan.errors import ScanError
from faultflow.scan.ring_generator import (
    LfsrPolynomial,
    default_ring_width,
    default_warmup,
    injector_stages,
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


_MASK64 = (1 << 64) - 1
# Draws per chain before a rotation of another chain's taps is accepted (a ring
# too short for every chain to have its own rotation class).
_ROTATION_DRAWS = 4096


def _splitmix64(state: int) -> tuple[int, int]:
    """One step of splitmix64 (Steele/Lea/Flood, 2014): the next state and its
    output. FaultFlow's own, so the phase shifter never depends on how a
    Python version's random module draws."""
    state = (state + 0x9E3779B97F4A7C15) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return state, z ^ (z >> 31)


def build_phase_shifter(width: int, num_chains: int, taps: int = 3) -> list[list[int]]:
    """The continuous decompressor's phase shifter: row c is the sorted ring stages
    whose XOR drives chain c's scan-in.

    Each chain reads ``taps`` distinct stages: the first spread evenly over the ring,
    so every stage is read once there are as many chains as stages, the others drawn
    by a fixed pseudo-random sequence -- the same rows for the same ring and chains,
    always (scan-compress and ATPG each build them). No two chains read the same
    stages, and while the ring has rotation classes enough, no chain's stages are a
    rotation of another's: the ring shifts its state one stage per shift, so a
    rotated tap set gives the same scan-ins a few shifts later, and its chain's cells
    would copy another chain's."""
    if num_chains <= 0:
        raise ValueError("num_chains must be positive")
    if not 1 <= taps <= width:
        raise ValueError(f"{taps} taps on a {width}-stage ring")
    state = (width << 32) ^ num_chains
    rows: list[list[int]] = []
    seen: set[frozenset[int]] = set()
    rotations: set[frozenset[int]] = set()
    for chain in range(num_chains):
        base = (chain * width) // num_chains if num_chains <= width else chain % width
        for draw in range(_ROTATION_DRAWS * 4):
            stages = {base}
            while len(stages) < taps:
                state, out = _splitmix64(state)
                stages.add(out % width)
            row = frozenset(stages)
            if row in seen:
                continue
            if row in rotations and draw < _ROTATION_DRAWS:
                continue
            break
        else:
            raise ValueError(
                f"{num_chains} chains: no {taps}-tap phase-shifter row left for chain "
                f"{chain} on a {width}-stage ring"
            )
        seen.add(row)
        rotations.update(frozenset((k + d) % width for k in row) for d in range(width))
        rows.append(sorted(row))
    return rows


def decompressor_wrapper_verilog(
    core_json: dict[str, Any],
    core_module: str,
    decompressor: "ContinuousDecompressor",
    wrapper_module: str,
    instance_name: str = "core_inst",
) -> str:
    """The wrapper RTL around ``core_module``: instantiate it by name (a real
    instantiation -- blackboxing happens later, in assemble.py's block_stub_verilog)
    with every port wired straight through but the decompressor's scan inputs, which
    its phase shifter drives, plus the channel port. ``instance_name`` must be the
    key the caller gives this core in assemble_soc()'s ``blocks`` /
    ``block_module`` (compose_soc looks the blackboxed instance up by it).

    The scan enable and the clock must already be ports of ``core_module`` -- the
    ring's clock is the design's existing one, so no new clock domain is introduced.
    Pure string templating: Yosys only sees the text later, as an input file."""
    scan_in_ports = decompressor.scan_in_ports
    channel_port = decompressor.channel_port
    modules = core_json.get("modules")
    if not isinstance(modules, dict) or core_module not in modules:
        raise ValueError(f"core JSON has no module {core_module!r}")
    ports = modules[core_module].get("ports", {})
    if not isinstance(ports, dict):
        raise ValueError(f"module {core_module!r} has no ports")

    scan_in_set = set(scan_in_ports)
    passthrough_names = [name for name in ports if name not in scan_in_set]
    decompressor.check_ports(passthrough_names, core_module)

    port_decls: list[str] = []
    port_list: list[str] = passthrough_names + [channel_port]
    for name in passthrough_names:
        port = ports[name]
        direction = port.get("direction", "input")
        port_width = len(port.get("bits", []))
        port_decls.append(_port_decl(name, direction, port_width))
    port_decls.append(_port_decl(channel_port, "input", decompressor.channel_width))

    wire_decls = [f"  wire {name};" for name in scan_in_ports]
    inst_conns = [f"    .{name}({name})" for name in passthrough_names + scan_in_ports]

    lines = [
        f"module {wrapper_module}({', '.join(port_list)});",
        *port_decls,
        "",
        *wire_decls,
        "",
        *decompressor.verilog(),
        "",
        f"  {core_module} {instance_name} (",
        ",\n".join(inst_conns),
        "  );",
        "endmodule",
        "",
    ]
    return "\n".join(lines)


def _check_rows(phase_shifter_taps: list[list[int]], scan_in_ports: list[str]) -> None:
    if len(phase_shifter_taps) != len(scan_in_ports):
        raise ValueError(
            f"fanout has {len(phase_shifter_taps)} rows but there are "
            f"{len(scan_in_ports)} scan_in_ports -- exactly one fanout "
            "row per scan chain is required"
        )


def _check_ports(
    decompressor: "ContinuousDecompressor",
    passthrough_names: list[str],
    core_module: str,
) -> None:
    """Scan enable and the clock must be ports the wrapper passes through to the
    core; the channel port must not be one."""
    if decompressor.scan_enable_port not in passthrough_names:
        raise ValueError(
            f"scan_enable_port {decompressor.scan_enable_port!r} is not a port of "
            f"{core_module!r} (or is itself one of scan_in_ports)"
        )
    if decompressor.clock_port not in passthrough_names:
        raise ValueError(
            f"clock_port {decompressor.clock_port!r} is not a port of "
            f"{core_module!r} (or is itself one of scan_in_ports)"
        )
    if decompressor.channel_port in passthrough_names:
        raise ValueError(
            f"the channel port {decompressor.channel_port!r} is a port of "
            f"{core_module!r} too; name the channels something else "
            "([compression] channel_port)"
        )


@dataclass(frozen=True)
class ContinuousDecompressor:
    """A continuous-flow decompressor in front of a scanned core's chains: a ring
    generator of ``polynomial.width`` stages, cleared on the first shift of each
    load (the scan enable's rise after a clock with it low) and fed ``num_channels``
    channel bits on every shift (ring_generator.injector_stages), and a phase
    shifter driving each of ``scan_in_ports``. A load is ``warmup`` shifts plus the
    longest chain's: the warm-up's scan-ins fall off the chains' far ends, and fill
    the ring with channel bits first."""

    scan_in_ports: list[str]
    polynomial: LfsrPolynomial
    phase_shifter_taps: list[list[int]]
    num_channels: int
    warmup: int
    channel_port: str = "tdi"
    scan_enable_port: str = "scan_en"
    clock_port: str = "clk"

    def __post_init__(self) -> None:
        _check_rows(self.phase_shifter_taps, self.scan_in_ports)
        injector_stages(self.polynomial.width, self.num_channels)
        if self.warmup < 1:
            raise ValueError(
                f"warmup is {self.warmup}; the clearing shift loads 0, so at least 1"
            )
        width = self.polynomial.width
        if any(not 0 <= t < width for row in self.phase_shifter_taps for t in row):
            raise ValueError(f"a phase-shifter tap is outside the {width}-stage ring")

    @property
    def injectors(self) -> list[int]:
        return injector_stages(self.polynomial.width, self.num_channels)

    @property
    def channel_width(self) -> int:
        """The channel bus's bits: one word of them applied every shift."""
        return self.num_channels

    def manifest_entry(self) -> dict[str, Any]:
        """What the scan manifest's ``compression`` entry records of this decompressor:
        all a later step needs to rebuild it (:meth:`from_manifest`) -- scan-compact
        composing the whole chip, the ATPG's cross-check, warptap retargeting a
        pattern. ``injectors`` is redundant with the ring and the channels, and
        checked against them."""
        return {
            "architecture": "continuous",
            "num_channels": self.num_channels,
            "ring_width": self.polynomial.width,
            "warmup": self.warmup,
            "injectors": self.injectors,
            "tap_source_net": "effective_state",
            "scan_in_ports": list(self.scan_in_ports),
            "phase_shifter_taps": [list(row) for row in self.phase_shifter_taps],
            "polynomial": {
                "width": self.polynomial.width,
                "taps": sorted(self.polynomial.taps),
            },
            "channel_port": self.channel_port,
            "scan_enable_port": self.scan_enable_port,
            "clock_port": self.clock_port,
        }

    @classmethod
    def from_manifest(cls, compression: dict[str, Any]) -> "ContinuousDecompressor":
        """The decompressor scan-compress composed, from its manifest entry
        (``manifest["compression"]``); an entry from a one-shot decompressor (no
        ``architecture``) or one that doesn't fit the model is refused."""
        if compression.get("architecture") != "continuous":
            raise ScanError(
                "the manifest's compression entry is not a continuous-flow "
                "decompressor's (a one-shot one, made before): run scan-compress again"
            )
        for key in (
            "num_channels",
            "warmup",
            "polynomial",
            "phase_shifter_taps",
            "scan_in_ports",
            "scan_enable_port",
            "clock_port",
        ):
            if key not in compression:
                raise ScanError(f"the manifest's compression entry has no {key}")
        polynomial = compression["polynomial"]
        decompressor = cls(
            [str(p) for p in compression["scan_in_ports"]],
            LfsrPolynomial(
                int(polynomial["width"]), frozenset(int(t) for t in polynomial["taps"])
            ),
            [[int(t) for t in row] for row in compression["phase_shifter_taps"]],
            int(compression["num_channels"]),
            int(compression["warmup"]),
            channel_port=str(compression.get("channel_port", "tdi")),
            scan_enable_port=str(compression["scan_enable_port"]),
            clock_port=str(compression["clock_port"]),
        )
        stated = compression.get("injectors")
        if stated is not None and [int(s) for s in stated] != decompressor.injectors:
            raise ScanError(
                f"the manifest's injectors {list(stated)} are not the stages "
                f"{decompressor.injectors} the model injects the channels into"
            )
        return decompressor

    def check_ports(self, passthrough_names: list[str], core_module: str) -> None:
        _check_ports(self, passthrough_names, core_module)

    def verilog(self) -> list[str]:
        return continuous_decompressor_verilog(self)


def continuous_decompressor_verilog(decompressor: ContinuousDecompressor) -> list[str]:
    """The continuous decompressor's lines in a wrapper module whose scan inputs are
    wires and whose channel port, scan enable and clock are ports. The ring
    generator's state is cleared on the scan enable's rise (``reseed``); on every
    shift it steps under its feedback taps and XORs each channel bit into its
    injector stage; the phase shifter reads the state after the clear (the model:
    ring_generator.continuous_scan_in_rows)."""
    width = decompressor.polynomial.width
    channel = decompressor.channel_port
    scan_enable = decompressor.scan_enable_port
    reg_width = f"[{width - 1}:0] "
    injected = {stage: c for c, stage in enumerate(decompressor.injectors)}

    def injected_source(stage: int) -> str:
        if stage not in injected:
            return "1'b0"
        if decompressor.num_channels == 1:
            return channel
        return f"{channel}[{injected[stage]}]"

    assigns = []
    for name, taps in zip(decompressor.scan_in_ports, decompressor.phase_shifter_taps):
        terms = " ^ ".join(f"effective_state[{t}]" for t in taps)
        assigns.append(f"  assign {name} = {terms};")
    mask = tap_mask_verilog_literal(decompressor.polynomial)
    return [
        f"  localparam {reg_width}TAP_MASK = {mask};",
        "",
        f"  reg {reg_width}lfsr_reg;",
        "  reg prev_scan_en;",
        f"  wire reseed = {scan_enable} & ~prev_scan_en;",
        f"  wire {reg_width}effective_state = reseed ? {width}'b0 : lfsr_reg;",
        f"  wire fb = effective_state[{width - 1}];",
        f"  wire {reg_width}injected;",
        *(
            f"  assign injected[{stage}] = {injected_source(stage)};"
            for stage in range(width)
        ),
        f"  wire {reg_width}next_state;",
        "  assign next_state[0] = fb ^ injected[0];",
        "  genvar i;",
        "  generate",
        f"    for (i = 1; i < {width}; i = i + 1) begin : lfsr_tap",
        "      assign next_state[i] = effective_state[i-1]"
        " ^ (TAP_MASK[i] ? fb : 1'b0) ^ injected[i];",
        "    end",
        "  endgenerate",
        "",
        f"  always @(posedge {decompressor.clock_port}) begin",
        f"    prev_scan_en <= {scan_enable};",
        f"    if ({scan_enable})",
        "      lfsr_reg <= next_state;",
        "  end",
        "",
        *assigns,
    ]


def _compose(
    wrapper_rtl: str,
    wrapper_top: str,
    core_json_path: Path,
    core_top: str,
    liberty: LibraryArg,
    output_json: Path,
    workdir: Path,
    instance_name: str,
) -> None:
    """Synthesize the wrapper around the frozen core and splice the core's cells
    back in (assemble_soc): one flat netlist at output_json."""
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


def build_continuous_decompressor(
    scan_in_ports: list[str],
    num_channels: int,
    *,
    ring_width: int | None = None,
    warmup: int | None = None,
    channel_port: str = "tdi",
    scan_enable_port: str = "scan_en",
    clock_port: str = "clk",
) -> ContinuousDecompressor:
    """The continuous decompressor for these scan inputs and channels: a ring of
    ``ring_width`` stages (a curated width, at least ``num_channels``; default
    ring_generator.default_ring_width), a load ``warmup`` shifts longer than the
    longest chain (default ring_generator.default_warmup), and the phase shifter
    build_phase_shifter's for that ring and these chains -- the same for the same
    inputs, so scan-compress and the ATPG each build it and agree."""
    width = ring_width if ring_width is not None else default_ring_width(num_channels)
    return ContinuousDecompressor(
        list(scan_in_ports),
        lookup_polynomial(width),
        build_phase_shifter(width, len(scan_in_ports)),
        num_channels,
        warmup if warmup is not None else default_warmup(width, num_channels),
        channel_port=channel_port,
        scan_enable_port=scan_enable_port,
        clock_port=clock_port,
    )


def insert_continuous_compression(
    core_json_path: Path,
    core_top: str,
    scan_in_ports: list[str],
    num_channels: int,
    liberty: LibraryArg,
    output_json: Path,
    *,
    workdir: Path,
    ring_width: int | None = None,
    warmup: int | None = None,
    clock_port: str = "clk",
    scan_enable_port: str = "scan_en",
    channel_port: str = "tdi",
) -> tuple[Path, ContinuousDecompressor]:
    """Insert a continuous-flow decompressor around a frozen, already scan-stitched
    core netlist, composing one flat netlist at ``output_json``: the core is the
    single "block" of an assemble_soc() composition (blackboxed for synthesis so
    Yosys's optimizer never touches it, spliced back with compose_soc) and the
    wrapper RTL the glue.

    The ring is ``ring_width`` stages (a curated width, at least ``num_channels``;
    default ring_generator.default_ring_width), a load ``warmup`` shifts longer
    than the longest chain (default ring_generator.default_warmup), and the phase
    shifter build_phase_shifter's for that ring and these chains. Returns the
    netlist's path and the decompressor."""
    decompressor = build_continuous_decompressor(
        scan_in_ports,
        num_channels,
        ring_width=ring_width,
        warmup=warmup,
        channel_port=channel_port,
        scan_enable_port=scan_enable_port,
        clock_port=clock_port,
    )
    core_json = json.loads(core_json_path.read_text(encoding="utf-8"))
    wrapper_top = f"{core_top}_compressed"
    instance_name = "core_inst"
    wrapper_rtl = decompressor_wrapper_verilog(
        core_json, core_top, decompressor, wrapper_top, instance_name
    )
    _compose(
        wrapper_rtl,
        wrapper_top,
        core_json_path,
        core_top,
        liberty,
        output_json,
        workdir,
        instance_name,
    )
    return output_json, decompressor
