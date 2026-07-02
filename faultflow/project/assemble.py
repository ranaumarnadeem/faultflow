"""Compose a flat, single-top Yosys-JSON SoC netlist from a glue synthesis plus
each block's own frozen, already-synthesized / scanned / wrapped generic JSON.

Each block is synthesized, scan-stitched, and IEEE-1500-wrapped standalone via
the locked Yosys script (see faultflow/scan/stitch.py, faultflow/wrap/ports.py) —
that JSON is the block's FROZEN, tested artifact and must never be re-synthesized
in the SoC context (abc/opt would freely restructure/merge gates across the block
boundary once flattened together with the rest of the chip).

Instead: (1) Yosys synthesizes ONLY the SoC glue (interconnect + block
instantiations), with each block module read via ``read_verilog -lib <stub>.v`` so
Yosys treats it as a blackbox and ``flatten`` has nothing to inline for it — the
block survives synthesis as an untouched instance cell with a ``connections`` dict
mapping each port to a glue-space net id list. (2) ``compose_soc`` splices each
block's real cells/netnames into the glue's instance site in pure Python,
net-ID-remapping the block's internal nets into the SoC's net-ID space and wiring
the block's port nets directly to the glue instance's connections. The result is
ONE flat module the C++ core can parse/normalize/compile/simulate unchanged.

WBC (IEEE-1500 wrapper boundary) cells copied from a block are re-tagged with
``attributes["faultflow_block"]`` / ``attributes["faultflow_wbc"]`` (plain Python
strings) so ``faultflow.project.aggregate._wbc_pin_index`` can recognize a
spliced-in block's wrapper cells as boundary faults owned by that block.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from faultflow.scan.wbr_view import (
    _WBR_IN_TYPES,
    _WBR_OUT_TYPES,
    _WBR_SCAN_IN_TYPES,
    _WBR_SCAN_OUT_TYPES,
)

_WBC_CELL_TYPES = (
    _WBR_IN_TYPES | _WBR_OUT_TYPES | _WBR_SCAN_IN_TYPES | _WBR_SCAN_OUT_TYPES
)

_TOP_ATTRS = ("1", "00000000000000000000000000000001", 1, True)


class AssembleError(RuntimeError):
    """SoC assembly (glue synthesis or composition) cannot proceed."""


def _all_int_bits(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [bit for bit in value if isinstance(bit, int)]


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _truthy_top(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return value in _TOP_ATTRS


def _find_top(data: dict[str, Any], requested_top: str) -> dict[str, Any]:
    modules = data.get("modules")
    if not isinstance(modules, dict):
        raise AssembleError("glue JSON is missing modules")
    module = modules.get(requested_top)
    if isinstance(module, dict):
        return module
    for mod in modules.values():
        if not isinstance(mod, dict):
            continue
        attrs = mod.get("attributes", {})
        if isinstance(attrs, dict) and _truthy_top(attrs.get("top")):
            return mod
    raise AssembleError(f"cannot find top module {requested_top!r} in glue JSON")


def block_stub_verilog(block_json: dict[str, Any], module: str) -> str:
    """Emit a blackbox Verilog interface stub for `module` from `block_json`.

    ``(* blackbox *) module <module>(...);`` with port declarations only (direction
    + width, derived from ``block_json['modules'][module]['ports']``) and no cells
    and no body -- pure string-building, no Yosys involved. When read via
    ``read_verilog -lib``, Yosys treats this as a blackbox: ``flatten`` has nothing
    to inline for it, so the real instance survives glue synthesis untouched.
    """
    modules = block_json.get("modules")
    if not isinstance(modules, dict) or module not in modules:
        raise AssembleError(f"block JSON has no module {module!r}")
    mod = modules[module]
    ports = mod.get("ports", {})
    if not isinstance(ports, dict):
        raise AssembleError(f"module {module!r} has no ports")

    port_names = list(ports.keys())
    decls: list[str] = []
    for name in port_names:
        port = ports[name]
        direction = port.get("direction", "input")
        width = len(port.get("bits", []))
        if width <= 1:
            decls.append(f"  {direction} {name};")
        else:
            decls.append(f"  {direction} [{width - 1}:0] {name};")

    header = f"module {module}({', '.join(port_names)});"
    lines = ["(* blackbox *)", header, *decls, "endmodule", ""]
    return "\n".join(lines)


def compose_soc(
    glue_json: dict[str, Any],
    soc_top: str,
    blocks: dict[str, dict[str, Any]],
    block_module: dict[str, str],
) -> dict[str, Any]:
    """Splice each block's real cells/netnames into the glue's instance site.

    Returns a NEW flat Yosys-JSON dict: one module (`soc_top`) containing the
    glue's own cells plus every block's cells (net-ID-remapped + spliced in), with
    each block's instance cell removed. See the module docstring for the algorithm.
    """
    glue_module = _find_top(glue_json, soc_top)
    cells = glue_module.get("cells")
    if not isinstance(cells, dict):
        raise AssembleError(f"top module {soc_top!r} has no cells")

    result_module: dict[str, Any] = {
        "attributes": dict(glue_module.get("attributes", {})),
        "ports": {k: dict(v) for k, v in glue_module.get("ports", {}).items()},
        "cells": {k: _deep_copy_cell(v) for k, v in cells.items()},
        "netnames": {
            k: _deep_copy_net(v) for k, v in glue_module.get("netnames", {}).items()
        },
    }

    # Running max net id, updated after each splice so blocks never collide.
    next_id = _next_net_id(result_module)

    for inst, block_json in blocks.items():
        module_name = block_module.get(inst)
        if module_name is None:
            raise AssembleError(f"no block_module entry for instance {inst!r}")
        inst_cell = result_module["cells"].get(inst)
        if not isinstance(inst_cell, dict):
            raise AssembleError(
                f"instance cell {inst!r} not found in glue top {soc_top!r}"
            )
        inst_connections = inst_cell.get("connections")
        if not isinstance(inst_connections, dict):
            raise AssembleError(f"instance cell {inst!r} has no connections")

        block_modules = block_json.get("modules")
        if not isinstance(block_modules, dict) or module_name not in block_modules:
            raise AssembleError(
                f"block for instance {inst!r} has no module {module_name!r}"
            )
        block_module_data = block_modules[module_name]

        remap, next_id = _build_remap(block_module_data, inst_connections, next_id)

        _splice_cells(result_module, block_module_data, inst, remap)
        _splice_netnames(result_module, block_module_data, inst, remap)

        del result_module["cells"][inst]

    return {
        "creator": glue_json.get("creator", "faultflow-assemble"),
        "modules": {soc_top: result_module},
    }


def _deep_copy_cell(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        "hide_name": cell.get("hide_name", 0),
        "type": cell.get("type", ""),
        "parameters": dict(cell.get("parameters", {})),
        "attributes": dict(cell.get("attributes", {})),
        "port_directions": dict(cell.get("port_directions", {})),
        "connections": {
            pin: list(bits) for pin, bits in cell.get("connections", {}).items()
        },
    }


def _deep_copy_net(net: dict[str, Any]) -> dict[str, Any]:
    return {
        "hide_name": net.get("hide_name", 0),
        "bits": list(net.get("bits", [])),
        "attributes": dict(net.get("attributes", {})),
    }


def _next_net_id(module: dict[str, Any]) -> int:
    max_id = -1
    for port in module.get("ports", {}).values():
        if isinstance(port, dict):
            max_id = max(max_id, *(_all_int_bits(port.get("bits")) or [-1]))
    for net in module.get("netnames", {}).values():
        if isinstance(net, dict):
            max_id = max(max_id, *(_all_int_bits(net.get("bits")) or [-1]))
    for cell in module.get("cells", {}).values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for bits in conns.values():
            max_id = max(max_id, *(_all_int_bits(bits) or [-1]))
    return max_id + 1


def _build_remap(
    block_module: dict[str, Any],
    inst_connections: dict[str, list[Any]],
    next_id: int,
) -> tuple[dict[int, int], int]:
    """Build net_id -> new_net_id for every int bit in the block module.

    Port-boundary bits map to the glue instance's connections (index-aligned for
    multi-bit ports). Every other (internal) bit gets a fresh sequential id from
    `next_id`, which is returned incremented past whatever was allocated.
    """
    block_ports = block_module.get("ports", {})
    if not isinstance(block_ports, dict):
        raise AssembleError("block module has no ports")

    remap: dict[int, int] = {}

    # Port-boundary nets first: they map directly to the glue instance's wiring.
    for port_name, port in block_ports.items():
        if not isinstance(port, dict):
            continue
        block_bits = port.get("bits", [])
        glue_bits = inst_connections.get(port_name)
        if glue_bits is None:
            raise AssembleError(f"instance connections missing port {port_name!r}")
        for i, bit in enumerate(block_bits):
            if not isinstance(bit, int):
                continue
            if i >= len(glue_bits):
                raise AssembleError(
                    f"port {port_name!r} width mismatch vs instance connections"
                )
            glue_bit = glue_bits[i]
            if isinstance(glue_bit, int):
                remap[bit] = glue_bit
            # else: glue side is a constant literal ("0"/"1"/"x"/"z") -- the
            # block's port bit still needs a remap target if referenced
            # internally, but with no glue-space net to bind to we fall back to
            # a fresh internal id below (handled by the "not in remap" pass).

    # Every other int bit anywhere in the block module gets a fresh id, unless
    # it was already bound to a port-boundary net above.
    all_bits: set[int] = set()
    for port in block_ports.values():
        if isinstance(port, dict):
            all_bits.update(_all_int_bits(port.get("bits")))
    for net in block_module.get("netnames", {}).values():
        if isinstance(net, dict):
            all_bits.update(_all_int_bits(net.get("bits")))
    for cell in block_module.get("cells", {}).values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections", {})
        if not isinstance(conns, dict):
            continue
        for bits in conns.values():
            all_bits.update(_all_int_bits(bits))

    for bit in sorted(all_bits):
        if bit not in remap:
            remap[bit] = next_id
            next_id += 1

    return remap, next_id


def _remap_bits(bits: list[Any], remap: dict[int, int]) -> list[Any]:
    return [remap[b] if isinstance(b, int) else b for b in bits]


def _splice_cells(
    result_module: dict[str, Any],
    block_module: dict[str, Any],
    inst: str,
    remap: dict[int, int],
) -> None:
    block_cells = block_module.get("cells", {})
    if not isinstance(block_cells, dict):
        return
    dest_cells = result_module["cells"]
    for cell_name, cell in block_cells.items():
        if not isinstance(cell, dict):
            continue
        new_cell = _deep_copy_cell(cell)
        new_cell["connections"] = {
            pin: _remap_bits(bits, remap)
            for pin, bits in new_cell["connections"].items()
        }
        cell_type = str(cell.get("type", ""))
        if cell_type in _WBC_CELL_TYPES:
            new_cell["attributes"]["faultflow_block"] = inst
            new_cell["attributes"]["faultflow_wbc"] = cell_name
        dest_cells[f"{inst}__{cell_name}"] = new_cell


def _splice_netnames(
    result_module: dict[str, Any],
    block_module: dict[str, Any],
    inst: str,
    remap: dict[int, int],
) -> None:
    block_ports = block_module.get("ports", {})
    port_names = set(block_ports) if isinstance(block_ports, dict) else set()
    block_netnames = block_module.get("netnames", {})
    if not isinstance(block_netnames, dict):
        return
    dest_netnames = result_module["netnames"]
    for net_name, net in block_netnames.items():
        if not isinstance(net, dict):
            continue
        # Port-level netnames are dropped: the port net already has a name in
        # the glue scope. Keep internal netnames (prefixed) for debuggability.
        if net_name in port_names:
            continue
        new_net = _deep_copy_net(net)
        new_net["bits"] = _remap_bits(new_net["bits"], remap)
        dest_netnames[f"{inst}__{net_name}"] = new_net


def assemble_soc(
    soc_rtl: Path,
    soc_top: str,
    liberty: Path,
    blocks: dict[str, Path],
    block_module: dict[str, str],
    output_json: Path,
    *,
    workdir: Path,
) -> Path:
    """Synthesize the SoC glue (blocks as blackboxes) and splice in each block's
    frozen JSON, producing one flat, single-top Yosys-JSON netlist at `output_json`.
    """
    yosys = shutil.which("yosys")
    if yosys is None:
        raise AssembleError("SoC assembly requires yosys on PATH")
    if not soc_rtl.exists():
        raise AssembleError(f"missing SoC glue RTL: {soc_rtl}")
    if not liberty.exists():
        raise AssembleError(f"missing liberty file: {liberty}")

    workdir.mkdir(parents=True, exist_ok=True)

    import json

    blocks_data: dict[str, dict[str, Any]] = {}
    stub_paths: list[Path] = []
    for inst, block_path in blocks.items():
        if not block_path.exists():
            raise AssembleError(
                f"missing block JSON for instance {inst!r}: {block_path}"
            )
        module_name = block_module.get(inst)
        if module_name is None:
            raise AssembleError(f"no block_module entry for instance {inst!r}")
        block_data = json.loads(block_path.read_text(encoding="utf-8"))
        blocks_data[inst] = block_data

        stub_verilog = block_stub_verilog(block_data, module_name)
        stub_path = workdir / f"{module_name}_stub.v"
        stub_path.write_text(stub_verilog, encoding="utf-8")
        stub_paths.append(stub_path)

    glue_json_path = workdir / f"{soc_top}_glue.json"
    log_path = workdir / f"{soc_top}_glue_synth.log"
    script_path = workdir / f"{soc_top}_glue_synth.tcl"

    lib_reads = " ".join(_quote(p) for p in stub_paths)
    script_lines = [
        f"read_verilog {_quote(soc_rtl)}",
    ]
    if stub_paths:
        script_lines.append(f"read_verilog -lib {lib_reads}")
    script_lines.extend(
        [
            f"hierarchy -check -top {soc_top}",
            "proc",
            "flatten",
            "opt_expr",
            "opt_clean",
            f"synth -top {soc_top}",
            f"dfflibmap -liberty {_quote(liberty)}",
            f"abc -liberty {_quote(liberty)}",
            "clean",
            f"write_json {_quote(glue_json_path)}",
        ]
    )
    script_path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [yosys, "-Q", "-s", str(script_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise AssembleError(f"SoC glue synthesis failed; see {log_path}")

    glue_json = json.loads(glue_json_path.read_text(encoding="utf-8"))
    composed = compose_soc(
        glue_json=glue_json,
        soc_top=soc_top,
        blocks=blocks_data,
        block_module=block_module,
    )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(composed, indent=2), encoding="utf-8")
    return output_json


__all__ = [
    "AssembleError",
    "assemble_soc",
    "block_stub_verilog",
    "compose_soc",
]
