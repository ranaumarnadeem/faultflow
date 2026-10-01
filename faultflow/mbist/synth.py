"""Synthesis of a design mbist-insert wrote, without ever optimizing inserted
logic together with the user's.

The DFT is synthesized frozen and nested:
1. Every leaf of a shell -- each module the shell or its collar instantiates
   directly: the MBIST controller, any repair or diagnosis logic, the
   synchronizers -- is synthesized alone.
2. Every shell is synthesized with its leaves and the memory macro as
   blackboxes, and the leaves' netlists are spliced in: one frozen shell.
3. The user's logic is synthesized with the shells as blackboxes, and the
   frozen shells are spliced in.

No Yosys pass runs on a spliced netlist again, so nothing optimizes across a
DFT boundary. A spliced cell is named `<instance>__<cell>`: the controller of
the shell at u_core0.u_mem is `u_core0.u_mem__u_collar.u_algo_top__<cell>`, its
memory `u_core0.u_mem__u_collar.u_sram`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.integrations.autombist import (
    _quote,
    _run_yosys,
    _synth_lines,
    parse_check_problems,
)
from faultflow.mbist.netlist import InsertError, _is_blackbox
from faultflow.project.assemble import compose_soc

Modules = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SynthesizedChip:
    composed_json: Path
    top: str
    # The memory macro instances of the composed netlist, inside shells or not.
    memories: tuple[str, ...]
    # Composed instance prefix -> the frozen module synthesized for it: each
    # shell (by its path) and each leaf (`<shell path>__<leaf instance>`).
    frozen: dict[str, str]
    # Standalone netlists, for the record: frozen module -> JSON.
    standalone: dict[str, Path]


def _reads(rtl: Path, libs: Sequence[Path]) -> list[str]:
    lines = [f"read_verilog -sv {_quote(rtl)}"]
    if libs:
        lines.append("read_verilog -lib " + " ".join(_quote(p) for p in libs))
    return lines


def _synthesize(
    rtl: Path,
    libs: Sequence[Path],
    top: str,
    blackboxes: Sequence[str],
    liberty: Path,
    out: Path,
) -> dict[str, Any]:
    """`top` from `rtl`, every module in `blackboxes` kept a blackbox, through
    the locked synthesis script."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_yosys(
        [
            *_reads(rtl, libs),
            *(f"blackbox {module}" for module in blackboxes),
            *_synth_lines(top, liberty, out),
        ],
        log_path=out.with_suffix(".log"),
        script_path=out.with_suffix(".ys"),
        error_message=f"synthesis of {top} failed",
    )
    data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return data


def _elaborated(rtl: Path, libs: Sequence[Path], top: str, out: Path) -> Modules:
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_yosys(
        [
            *_reads(rtl, libs),
            f"hierarchy -check -top {top}",
            "proc",
            f"write_json {_quote(out)}",
        ],
        log_path=out.with_suffix(".log"),
        script_path=out.with_suffix(".ys"),
        error_message=f"Yosys could not read {rtl}",
    )
    modules: Modules = json.loads(out.read_text(encoding="utf-8"))["modules"]
    return modules


def shell_leaves(modules: Modules, shell: str, collar: str) -> dict[str, str]:
    """The leaf instances of `shell`, by their path in the shell's flattened
    netlist (instance -> module): what the shell and its collar instantiate
    directly, but the collar itself and the memory macro."""
    leaves: dict[str, str] = {}
    for cell_name, cell in modules[shell].get("cells", {}).items():
        ctype = cell.get("type", "")
        if ctype == collar:
            for inner, sub in modules[collar].get("cells", {}).items():
                sub_type = sub.get("type", "")
                if sub_type in modules and not _is_blackbox(modules[sub_type]):
                    leaves[f"{cell_name}.{inner}"] = sub_type
        elif ctype in modules and not _is_blackbox(modules[ctype]):
            leaves[cell_name] = ctype
    return leaves


def _cells_of(glue: dict[str, Any], top: str) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = glue["modules"][top].get("cells", {})
    return cells


def _check(
    netlist: Path, top: str, liberty: Path, libs: Sequence[Path] = ()
) -> tuple[str, ...]:
    """The messages Yosys check reports for `netlist`. `libs` declare the
    blackboxes the netlist instantiates but doesn't define: a netlist Yosys
    wrote defines its own, a composed one doesn't."""
    log = netlist.with_suffix(".check.log")
    _run_yosys(
        [
            f"read_liberty -lib {_quote(liberty)}",
            *(
                ["read_verilog -lib " + " ".join(_quote(p) for p in libs)]
                if libs
                else []
            ),
            f"read_json {_quote(netlist)}",
            f"hierarchy -top {top}",
            "check",
        ],
        log_path=log,
        script_path=netlist.with_suffix(".check.ys"),
        error_message=f"Yosys check of {netlist} failed to run",
    )
    return tuple(p.message for p in parse_check_problems(log.read_text("utf-8")))


def synthesize_inserted(
    rtl: Path,
    top: str,
    shells: dict[str, tuple[str, str]],
    *,
    libs: Sequence[Path],
    liberty: Path,
    workdir: Path,
) -> SynthesizedChip:
    """Synthesize the design mbist-insert wrote (`rtl`, top `top`), DFT frozen
    and nested. `shells` maps every shell instance path to its (shell module,
    collar module); `libs` are the design's macro stubs."""
    modules = _elaborated(rtl, libs, top, workdir / "elaborated.json")
    shell_modules = {module for module, _ in shells.values()}
    standalone: dict[str, Path] = {}
    frozen: dict[str, str] = {}

    # 1-2. Each shell module once: its leaves alone, then the shell around them.
    composed_shells: dict[str, dict[str, Any]] = {}
    leaf_json: dict[str, dict[str, Any]] = {}
    shell_leaf_paths: dict[str, dict[str, str]] = {}
    for shell, collar in sorted(set(shells.values())):
        leaves = shell_leaves(modules, shell, collar)
        for module in sorted(set(leaves.values())):
            if module not in leaf_json:
                out = workdir / "leaves" / f"leaf{len(leaf_json)}.json"
                leaf_json[module] = _synthesize(rtl, libs, module, (), liberty, out)
                standalone[module] = out
        glue_out = workdir / "shells" / f"{shell}.glue.json"
        glue = _synthesize(
            rtl, libs, shell, sorted(set(leaves.values())), liberty, glue_out
        )
        cells = _cells_of(glue, shell)
        lost = sorted(
            path for path, m in leaves.items() if cells.get(path, {}).get("type") != m
        )
        if lost:
            raise InsertError(f"synthesis of {shell} lost its leaf instances {lost}")
        composed_shells[shell] = compose_soc(
            glue_json=glue,
            soc_top=shell,
            blocks={path: leaf_json[m] for path, m in leaves.items()},
            block_module=dict(leaves),
        )
        out = workdir / "shells" / f"{shell}.json"
        out.write_text(json.dumps(composed_shells[shell]), encoding="utf-8")
        standalone[shell] = out
        shell_leaf_paths[shell] = leaves

    # 3. The user's logic around the shells.
    glue_out = workdir / f"{top}.glue.json"
    glue = _synthesize(rtl, libs, top, sorted(shell_modules), liberty, glue_out)
    cells = _cells_of(glue, top)
    lost = sorted(
        p for p, (m, _) in shells.items() if cells.get(p, {}).get("type") != m
    )
    if lost:
        raise InsertError(f"synthesis of {top} lost the shell instances {lost}")
    baseline = set(_check(glue_out, top, liberty))
    composed = compose_soc(
        glue_json=glue,
        soc_top=top,
        blocks={p: composed_shells[m] for p, (m, _) in shells.items()},
        block_module={p: m for p, (m, _) in shells.items()},
    )
    composed_json = workdir / f"{top}_composed.json"
    composed_json.write_text(json.dumps(composed, indent=1), encoding="utf-8")
    new = [m for m in _check(composed_json, top, liberty, libs) if m not in baseline]
    if new:
        raise InsertError(
            "the composed netlist has problems its parts didn't: "
            + "; ".join(new[:10])
            + " -- a connection was lost splicing the shells in"
        )

    for path, (shell, _) in shells.items():
        frozen[path] = shell
        for leaf_path, module in shell_leaf_paths[shell].items():
            frozen[f"{path}__{leaf_path}"] = module
    macros = {name for name, m in modules.items() if _is_blackbox(m)}
    memories = tuple(
        sorted(
            name
            for name, cell in composed["modules"][top]["cells"].items()
            if cell.get("type") in macros
        )
    )
    return SynthesizedChip(
        composed_json=composed_json,
        top=top,
        memories=memories,
        frozen=frozen,
        standalone=standalone,
    )
