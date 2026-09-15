"""Scan test-response compaction: static XOR-tree space compactor.

Python orchestration layer for the output-side counterpart to
``faultflow.scan.compression`` (input-side decompression). Where the
decompressor expands K external channels into N internal scan chains via a
sequential ring generator, this compacts N internal scan-chain outputs down
to K external channels via a single-cycle, purely combinational XOR tree --
no register, no clock, no sequential accumulation (a MISR). See the
compaction plan for the full architecture and IP-landscape rationale for
choosing a static space compactor over a MISR.

Modeled on the deterministic, never-patented "space compactor" concept --
Saluja & Karpovsky, "Testing Computer Hardware Through Data Compression in
Space and Time," Proc. ITC 1983 -- not implemented from, or modeled on, any
patent's claim language.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.project.assemble import assemble_soc


def _port_decl(name: str, direction: str, width: int) -> str:
    """Match block_stub_verilog's exact port-declaration formatting
    (faultflow/project/assemble.py:105-112), same convention already used by
    faultflow.scan.compression._port_decl -- duplicated (not imported) to
    keep the decompression and compaction modules independent."""
    if width <= 1:
        return f"  {direction} {name};"
    return f"  {direction} [{width - 1}:0] {name};"


def build_compactor_fanout(num_outputs: int, num_internal_bits: int) -> list[list[int]]:
    """``fanout[o]`` = sorted list of internal scan-chain-output indices
    XORed into compactor output bit ``o`` -- the observability-direction
    mirror of ``faultflow.scan.compression.build_broadcast_fanout`` (one row
    per FEW output bit here, instead of one row per MANY internal bit
    there -- the size relationship is necessarily reversed for a compactor,
    which is why this is a new function rather than a reuse of that one).

    Deliberately simple, deterministic construction (same
    placeholder-construction discipline as ``build_broadcast_fanout`` --
    testable, structurally sound, not literature-tuned): chain ``c``
    (0-indexed) is assigned the ``(c % period) + 1``-th non-zero
    ``num_outputs``-bit value as its "column" (which output bits it
    contributes to), where ``period = 2**num_outputs - 1`` is the count of
    non-zero ``num_outputs``-bit values.

    This guarantees, for any ``num_internal_bits``, every chain has a
    non-zero column -- a lone differing chain always changes at least one
    compacted output bit, never silently canceling to zero. Within one full
    cycle (``num_internal_bits <= period``), columns are additionally all
    DISTINCT -- no two chains are structurally indistinguishable to the
    compactor. Every output bit is touched by at least one chain once
    ``num_internal_bits >= 2**(num_outputs - 1)`` (the powers-of-two columns
    1, 2, 4, ..., ``2**(num_outputs-1)`` individually touch every bit
    position in turn). Beyond one full cycle, columns repeat -- an expected,
    practical chain-count-per-output-count limit, the same spirit as the
    decompressor's curated-channel-width limit.
    """
    if num_outputs <= 0:
        raise ValueError("num_outputs must be positive")
    if num_internal_bits <= 0:
        raise ValueError("num_internal_bits must be positive")

    period = (1 << num_outputs) - 1
    fanout: list[list[int]] = [[] for _ in range(num_outputs)]
    for c in range(num_internal_bits):
        column = (c % period) + 1
        for o in range(num_outputs):
            if (column >> o) & 1:
                fanout[o].append(c)
    return fanout


def compactor_wrapper_verilog(
    core_json: dict[str, Any],
    core_module: str,
    scan_out_ports: list[str],
    fanout: list[list[int]],
    wrapper_module: str,
    channel_port: str = "tdo",
    instance_name: str = "core_inst",
) -> str:
    """Emit compactor wrapper RTL: instantiate ``core_module`` by name (a
    real instantiation -- blackboxing happens later, in assemble.py's
    block_stub_verilog, not here) with every port wired straight through
    except ``scan_out_ports``, which become internal wires feeding a static
    combinational XOR tree that drives an external, ``len(fanout)``-wide
    ``channel_port`` output bus per ``fanout``.

    ``scan_out_ports`` is the ordered list of the design's actual scan-chain
    output port names (one per chain), and ``fanout[o]`` indexes INTO
    ``scan_out_ports`` (not a 1:1 row-per-chain correspondence like the
    decompressor's phase-shifter -- a compactor has fewer outputs than
    chains by construction, so ``len(fanout)`` is the (smaller) channel
    count, never required to equal ``len(scan_out_ports)``).

    Pure string templating -- no Yosys involved in generating this text;
    Yosys only sees it later as an input file to synthesize.

    ``instance_name`` MUST match whatever key the caller uses for this core
    in ``assemble_soc()``'s ``blocks``/``block_module`` dicts, exactly as in
    ``compression.py``'s ``ring_generator_wrapper_verilog`` -- see that
    function's docstring for the full rationale (unchanged here).
    """
    for row in fanout:
        for chain in row:
            if not (0 <= chain < len(scan_out_ports)):
                raise ValueError(
                    f"fanout references chain index {chain}, out of range "
                    f"for {len(scan_out_ports)} scan_out_ports"
                )

    modules = core_json.get("modules")
    if not isinstance(modules, dict) or core_module not in modules:
        raise ValueError(f"core JSON has no module {core_module!r}")
    ports = modules[core_module].get("ports", {})
    if not isinstance(ports, dict):
        raise ValueError(f"module {core_module!r} has no ports")

    scan_out_set = set(scan_out_ports)
    passthrough_names = [name for name in ports if name not in scan_out_set]

    port_decls: list[str] = []
    port_list: list[str] = passthrough_names + [channel_port]
    for name in passthrough_names:
        port = ports[name]
        direction = port.get("direction", "input")
        width = len(port.get("bits", []))
        port_decls.append(_port_decl(name, direction, width))
    port_decls.append(_port_decl(channel_port, "output", len(fanout)))

    wire_decls = [f"  wire {name};" for name in scan_out_ports]
    assigns = []
    for o, row in enumerate(fanout):
        terms = " ^ ".join(f"{scan_out_ports[c]}" for c in row)
        target = channel_port if len(fanout) <= 1 else f"{channel_port}[{o}]"
        assigns.append(f"  assign {target} = {terms};")

    inst_conns = [f"    .{name}({name})" for name in passthrough_names + scan_out_ports]

    lines = [
        f"module {wrapper_module}({', '.join(port_list)});",
        *port_decls,
        "",
        *wire_decls,
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
class CompactionMap:
    """The structure a caller needs to persist into a manifest and to check
    candidate observability against (via ``_check_compaction_distinguishable``
    in ``detection_pipeline.py``) after ``insert_compaction`` runs."""

    num_outputs: int
    fanout: list[list[int]]


def insert_compaction(
    core_json_path: Path,
    core_top: str,
    scan_out_ports: list[str],
    num_outputs: int,
    liberty: Path,
    output_json: Path,
    *,
    workdir: Path,
) -> tuple[Path, CompactionMap]:
    """Insert a static XOR-tree space compactor around a frozen, already
    scan-stitched core netlist, producing one flat composed netlist at
    ``output_json``.

    Reuses faultflow.project.assemble.assemble_soc() UNCHANGED, the exact
    same reuse shape as faultflow.scan.compression.insert_compression: the
    core design plays the single "block" (blackboxed via block_stub_verilog
    so Yosys's optimizer never touches it, spliced back in post-synthesis
    via compose_soc), and the newly-generated compactor wrapper RTL plays the
    SoC glue.

    ``scan_out_ports`` must be sourced by the caller (typically from the
    design's scan manifest). Returns the composed netlist path (==
    ``output_json``) and a ``CompactionMap`` describing the compactor's
    structure, needed for the observability check and for persisting into a
    manifest.
    """
    core_json = json.loads(core_json_path.read_text(encoding="utf-8"))
    fanout = build_compactor_fanout(num_outputs, len(scan_out_ports))
    wrapper_top = f"{core_top}_compacted"
    instance_name = "core_inst"
    wrapper_rtl = compactor_wrapper_verilog(
        core_json,
        core_top,
        scan_out_ports,
        fanout,
        wrapper_top,
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
    return output_json, CompactionMap(num_outputs, fanout)
