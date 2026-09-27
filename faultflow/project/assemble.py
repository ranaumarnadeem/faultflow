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
block's real cells/netnames into the glue's instance site in pure Python. Every
block port bit and the glue connection bit at the same index are ONE electrical
net; a block's own internal wiring or the wrapper's own wiring can tie that one
net to more than one boundary position (a shared clock net exposed on two port
names, a bus bit repeated across ports, a constant-tied input, a constant block
output). ``compose_soc`` resolves this via a union-find spanning the WHOLE call —
never a flat per-instance dict, which silently drops every boundary position but
the last one written. See ``_UnionFind`` / ``_collect_boundary_bindings`` /
``_resolve_classes`` / ``_apply_glue_substitution`` below. The result is ONE flat
module the C++ core can parse/normalize/compile/simulate unchanged — verified
driver-clean by the ``check -assert`` guard in
``faultflow/integrations/autombist.py::synthesize_from_manifest``.

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


def _verilog_parameter_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def block_stub_verilog(
    block_json: dict[str, Any],
    module: str,
    *,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Emit a blackbox Verilog interface stub for `module` from `block_json`.

    ``(* blackbox *) module <module>(...);`` with port declarations only (direction
    + width, derived from ``block_json['modules'][module]['ports']``) and no cells
    and no body -- pure string-building, no Yosys involved. When read via
    ``read_verilog -lib``, Yosys treats this as a blackbox: ``flatten`` has nothing
    to inline for it, so the real instance survives glue synthesis untouched.

    ``parameters``, when non-empty, adds a ``#(parameter K = V, ...)`` clause --
    REQUIRED when the real module has parameters and the glue instantiates it with
    an override (e.g. ``algo_top #(.ADDR_WIDTH(ADDR_WIDTH)) u_algo(...)``); without
    the declaration Yosys errors with "Module `X' ... does not have a parameter
    named 'K'". No type keyword is emitted (bare ``parameter K = V``) -- purely
    cosmetic for Yosys's own chparam/instantiation-override handling, and guessing
    one from a bare JSON scalar risks getting it wrong for a case the caller's data
    doesn't disambiguate. `parameters=None` (the default) produces output identical
    to before this argument existed.
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

    param_clause = ""
    if parameters:
        param_decls = ", ".join(
            f"parameter {k} = {_verilog_parameter_literal(v)}"
            for k, v in parameters.items()
        )
        param_clause = f" #({param_decls})"

    header = f"module {module}{param_clause}({', '.join(port_names)});"
    lines = ["(* blackbox *)", header, *decls, "endmodule", ""]
    return "\n".join(lines)


class _UnionFind:
    """A minimal union-find over arbitrary hashable keys (path compression only
    -- no union-by-rank; realistic netlists are small enough that this is not a
    performance concern). Keys are never pre-registered: `find`/`union` register
    an unseen key as its own singleton root on first use."""

    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def classes(self) -> dict[Any, set[Any]]:
        groups: dict[Any, set[Any]] = {}
        for key in self._parent:
            groups.setdefault(self.find(key), set()).add(key)
        return groups


def _collect_boundary_bindings(
    uf: _UnionFind,
    inst: str,
    block_module: dict[str, Any],
    inst_connections: dict[str, Any],
) -> set[int]:
    """Union every connected port-boundary bit of `inst` with its glue-side
    binding, and return the set of the block's OWN int bit values that were
    bound (used by the caller to tell a boundary bit from a purely-internal
    one when building that instance's remap).

    Block side: `("B", inst, bit)` for an int bit, or `("C", bit)` if the
    block's own port bit is itself a constant literal ("0"/"1"/"x"/"z") --
    global, not instance-scoped, since "the constant 1" is the same value
    regardless of which instance produced it.
    Glue side: `("G", net)` for an int net, or `("C", net)` if the glue ties
    the port to a constant.

    The tag is ALWAYS element 0 of every key, so classification can test
    `key[0]` alone. An instance-first block key (`(inst, "B", bit)`) would
    make an instance literally named "C" or "G" read as a constant or glue
    key.

    Unconnected ports (key absent from `inst_connections`, or present with an
    empty list -- both are real Yosys shapes, see `git blame` on this
    function) are skipped entirely: an unconnected input floats, which is
    valid Verilog. Raises AssembleError on a width mismatch between the
    block's own port and what the glue instantiation connects to it.
    """
    block_ports = block_module.get("ports", {})
    if not isinstance(block_ports, dict):
        raise AssembleError("block module has no ports")

    bound: set[int] = set()
    for port_name, port in block_ports.items():
        if not isinstance(port, dict):
            continue
        block_bits = port.get("bits", [])
        glue_bits = inst_connections.get(port_name)
        if not glue_bits:
            continue
        if len(glue_bits) != len(block_bits):
            raise AssembleError(
                f"instance {inst!r} port {port_name!r}: width mismatch "
                f"(block has {len(block_bits)} bit(s), glue connects "
                f"{len(glue_bits)})"
            )
        for i, bit in enumerate(block_bits):
            glue_bit = glue_bits[i]
            if isinstance(bit, int):
                block_key: tuple[Any, ...] = ("B", inst, bit)
                bound.add(bit)
            else:
                block_key = ("C", bit)
            glue_key = ("G", glue_bit) if isinstance(glue_bit, int) else ("C", glue_bit)
            uf.union(block_key, glue_key)
    return bound


def _resolve_classes(
    uf: _UnionFind, top_level_inputs: set[int]
) -> tuple[dict[Any, int | str], dict[Any, set[Any]]]:
    """Resolve every union-find class to one canonical value.

    - A class containing a constant resolves to that constant. Both "0" and
      "1" present is a contradiction (AssembleError); a top-level input ALSO
      tied to a constant is a contradiction too (an externally-driven pin
      can't also be fixed).
    - Else a class containing a top-level (primary) INPUT port bit resolves
      to that bit -- it is the one member guaranteed to stay externally
      driven no matter which instance cells get spliced/deleted. Two or more
      DIFFERENT top-level input bits in one class is a short (AssembleError).
      Otherwise resolves to the lowest glue net id in the class.
    - A class with no glue/constant member at all (a purely internal block
      bit, or a genuinely-unconnected port bit) is left unresolved here --
      the caller assigns it a fresh id.

    Returns `(resolved, classes)`: `resolved` maps a class's root to its
    canonical value; `classes` maps that same root to its full member set
    (reused by `_glue_substitution_map`, so the union-find is only walked
    once).
    """
    classes = uf.classes()
    resolved: dict[Any, int | str] = {}
    for root, members in classes.items():
        constants = sorted({k[1] for k in members if k[0] == "C"})
        glue_nets = {k[1] for k in members if k[0] == "G"}
        if constants:
            if "0" in constants and "1" in constants:
                raise AssembleError(
                    "conflicting constant drivers (0 and 1) tied to the same net"
                )
            value = constants[0]
            shorted_inputs = glue_nets & top_level_inputs
            if shorted_inputs:
                raise AssembleError(
                    f"top-level input net(s) {sorted(shorted_inputs)} tied to "
                    f"constant {value!r}"
                )
            resolved[root] = value
        elif glue_nets:
            shorted_inputs = glue_nets & top_level_inputs
            if len(shorted_inputs) > 1:
                raise AssembleError(
                    "top-level input nets shorted together: "
                    f"{sorted(shorted_inputs)}"
                )
            resolved[root] = (
                next(iter(shorted_inputs)) if shorted_inputs else min(glue_nets)
            )
        # else: no glue/constant member in this class -- left unresolved.
    return resolved, classes


def _instance_remap(
    inst: str,
    block_module: dict[str, Any],
    bound_bits: set[int],
    uf: _UnionFind,
    resolved: dict[Any, int | str],
    next_id: int,
) -> tuple[dict[int, int | str], int]:
    """Build bit -> remap target for every int bit in `block_module`.

    A boundary bit (in `bound_bits`) maps to its class's resolved value (an
    int glue net, or a constant string). Every other bit -- purely internal,
    or a port bit whose port was genuinely unconnected -- gets a fresh
    sequential id from `next_id`, which is returned incremented past
    whatever was allocated.
    """
    all_bits: set[int] = set()
    block_ports = block_module.get("ports", {})
    if isinstance(block_ports, dict):
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
        if isinstance(conns, dict):
            for bits in conns.values():
                all_bits.update(_all_int_bits(bits))

    remap: dict[int, int | str] = {}
    for bit in sorted(all_bits):
        if bit in bound_bits:
            root = uf.find(("B", inst, bit))
            remap[bit] = resolved[root]
        else:
            remap[bit] = next_id
            next_id += 1
    return remap, next_id


def _glue_substitution_map(
    resolved: dict[Any, int | str], classes: dict[Any, set[Any]]
) -> dict[int, int | str]:
    """Every glue net whose class resolved to something other than itself (a
    merge with another glue net, or a constant) needs substituting wherever
    the ORIGINAL glue netlist referenced it directly -- an existing glue-native
    cell/port/netname never goes through `_instance_remap`, so without this
    pass it would keep pointing at a net id that this composition just
    renamed out from under it."""
    substitution: dict[int, int | str] = {}
    for root, value in resolved.items():
        for key in classes[root]:
            if key[0] == "G" and key[1] != value:
                substitution[key[1]] = value
    return substitution


def _substitute_bits(bits: list[Any], substitution: dict[int, int | str]) -> list[Any]:
    return [substitution.get(b, b) if isinstance(b, int) else b for b in bits]


def _apply_glue_substitution(
    result_module: dict[str, Any], substitution: dict[int, int | str]
) -> None:
    """Rewrite every occurrence of a substituted glue net, across ALL of
    `result_module`'s cells' connections, ports' bits, and netnames' bits --
    both glue-native entries (which reference the OLD id directly) and
    already-spliced block cells (whose remap already used the resolved
    value, so this is a no-op for them; safe to apply uniformly)."""
    if not substitution:
        return
    for cell in result_module.get("cells", {}).values():
        if not isinstance(cell, dict):
            continue
        conns = cell.get("connections")
        if isinstance(conns, dict):
            for pin, bits in conns.items():
                if isinstance(bits, list):
                    conns[pin] = _substitute_bits(bits, substitution)
    for port in result_module.get("ports", {}).values():
        if isinstance(port, dict) and isinstance(port.get("bits"), list):
            port["bits"] = _substitute_bits(port["bits"], substitution)
    for net in result_module.get("netnames", {}).values():
        if isinstance(net, dict) and isinstance(net.get("bits"), list):
            net["bits"] = _substitute_bits(net["bits"], substitution)


def compose_soc(
    glue_json: dict[str, Any],
    soc_top: str,
    blocks: dict[str, dict[str, Any]],
    block_module: dict[str, str],
    *,
    graybox: bool = False,
    block_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Splice each block's real cells/netnames into the glue's instance site.

    Returns a NEW flat Yosys-JSON dict: one module (`soc_top`) containing the
    glue's own cells plus every block's cells (net-ID-remapped + spliced in), with
    each block's instance cell removed. See the module docstring for the algorithm
    -- in short, a union-find spans the WHOLE call so that a net shared across
    multiple port-boundary positions (a fanned-out clock exposed on two port
    names, a repeated bus bit, a constant tie on either side) resolves to one
    correct value everywhere, instead of a flat per-instance dict silently
    keeping only the last position written.

    ``graybox``: splice ONLY each block's WBC (IEEE-1500 wrapper) cells, dropping
    its core logic + internal scan FFs. This is the EXTEST view -- the wrapper
    boundary + interconnect is the DUT, the core is held dead -- and it keeps the
    fault set (and the scan manifest) free of dead-core noise / internal chains.

    ``block_names``: instance -> the block's canonical name. WBC cells are tagged
    ``faultflow_block`` with this name so aggregation's cross-scope fault keys line
    up with the block INTEST scope's name; defaults to the instance name.
    """
    glue_module = _find_top(glue_json, soc_top)
    cells = glue_module.get("cells")
    if not isinstance(cells, dict):
        raise AssembleError(f"top module {soc_top!r} has no cells")
    names = block_names or {}

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

    top_level_inputs = {
        b
        for port in glue_module.get("ports", {}).values()
        if isinstance(port, dict) and port.get("direction") == "input"
        for b in _all_int_bits(port.get("bits"))
    }

    # ---- Phase A: gather every instance's data + the boundary union-find ----
    uf = _UnionFind()
    block_module_data: dict[str, dict[str, Any]] = {}
    bound_bits: dict[str, set[int]] = {}

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
        module_data = block_modules[module_name]
        block_module_data[inst] = module_data
        bound_bits[inst] = _collect_boundary_bindings(
            uf, inst, module_data, inst_connections
        )

    # ---- Phase B: resolve every class to one canonical value ----
    resolved, classes = _resolve_classes(uf, top_level_inputs)

    # ---- Phase C: per-instance remap + splice (fresh ids assigned here) ----
    for inst in blocks:
        module_data = block_module_data[inst]
        remap, next_id = _instance_remap(
            inst, module_data, bound_bits[inst], uf, resolved, next_id
        )
        _splice_cells(
            result_module,
            module_data,
            inst,
            remap,
            graybox=graybox,
            block_tag=names.get(inst, inst),
        )
        _splice_netnames(result_module, module_data, inst, remap)
        del result_module["cells"][inst]

    # ---- Phase D: substitute every merged/constant-resolved glue net ----
    substitution = _glue_substitution_map(resolved, classes)
    _apply_glue_substitution(result_module, substitution)

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


def _remap_bits(bits: list[Any], remap: dict[int, Any]) -> list[Any]:
    return [remap[b] if isinstance(b, int) else b for b in bits]


def _splice_cells(
    result_module: dict[str, Any],
    block_module: dict[str, Any],
    inst: str,
    remap: dict[int, Any],
    *,
    graybox: bool = False,
    block_tag: str | None = None,
) -> None:
    block_cells = block_module.get("cells", {})
    if not isinstance(block_cells, dict):
        return
    tag = block_tag if block_tag is not None else inst
    dest_cells = result_module["cells"]
    for cell_name, cell in block_cells.items():
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("type", ""))
        is_wbc = cell_type in _WBC_CELL_TYPES
        # Graybox EXTEST view: keep only the wrapper boundary cells (the DUT); the
        # dead core's logic + internal scan FFs are dropped.
        if graybox and not is_wbc:
            continue
        new_cell = _deep_copy_cell(cell)
        new_cell["connections"] = {
            pin: _remap_bits(bits, remap)
            for pin, bits in new_cell["connections"].items()
        }
        if is_wbc:
            new_cell["attributes"]["faultflow_block"] = tag
            new_cell["attributes"]["faultflow_wbc"] = cell_name
        dest_cells[f"{inst}__{cell_name}"] = new_cell


def _splice_netnames(
    result_module: dict[str, Any],
    block_module: dict[str, Any],
    inst: str,
    remap: dict[int, Any],
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
    graybox: bool = False,
    block_names: dict[str, str] | None = None,
) -> Path:
    """Synthesize the SoC glue (blocks as blackboxes) and splice in each block's
    frozen JSON, producing one flat, single-top Yosys-JSON netlist at `output_json`.

    ``graybox``/``block_names`` are forwarded to `compose_soc` unchanged -- see its
    docstring. Use ``graybox=True`` for a scan-model EXTEST DUT (WBC ring only, dead
    core dropped) and ``block_names`` to tag WBC cells with each block's canonical
    name (matching the block INTEST scope) rather than its glue instance name.
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
        graybox=graybox,
        block_names=block_names,
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
