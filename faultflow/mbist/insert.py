"""mbist-insert: put an autoMBIST collar, in a FaultFlow shell, in place of
every memory the insertion file configures, keeping the design's hierarchy.

1. Read the insertion file and each memory's autoMBIST config.
2. Elaborate the design (Yosys, nothing optimized); keep what `check` reports.
3. Find every memory instance and verify its pins against the design.
4. Run `autombist generate --algo` once per (config, algorithm).
5. Build a shell per collar; the pins outside the collar's roles are driven in
   a copy of the collar per distinct set of ties.
6. Merge the shells' modules into the design (no name may collide), give every
   configured path its own module copies, swap each macro for its shell and
   thread the test ports up and the chip reset down.
7. Write the design back with Yosys, and elaborate it again: `check` may report
   no problem the original design didn't have.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.integrations.autombist import (
    CheckProblem,
    _quote,
    _run_yosys,
    invoke_autombist_generate,
    load_autombist_manifest,
)
from faultflow.mbist.autombist_config import CollarConfig, load_collar_config
from faultflow.mbist.netlist import (
    InsertError,
    _is_blackbox,
    locate,
    swap_and_thread,
    uniquify_path,
    verify_pins,
)
from faultflow.mbist.shell import (
    LIBRARY_VERILOG,
    CollarPort,
    collar_ports,
    shell_verilog,
)
from faultflow.mbist.spec import DesignSources, MbistSpec, MemorySpec, load_mbist_spec
from faultflow.mbist.yosys import elaborate

# Cells a design written back by Yosys can't carry faithfully, or that FaultFlow
# can't model (tri-state).
_REFUSED_CELLS = ("$print", "$check", "$assert", "$assume", "$tribuf", "$_TBUF_")

Modules = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class InsertedMemory:
    memory: str
    instance: str  # also the shell's instance path
    shell: str
    collar: str
    top_ports: dict[str, str]  # collar port -> top port


@dataclass(frozen=True)
class InsertResult:
    rtl: Path
    report: Path
    top: str
    memories: tuple[InsertedMemory, ...]
    copies: dict[str, str] = field(default_factory=dict)  # copy -> original


@dataclass
class _Group:
    """The memories sharing one generated collar: one config, one algorithm."""

    config: CollarConfig
    algo: str | None
    memories: list[MemorySpec]


def liberty_cells(liberty: Path) -> frozenset[str]:
    """The cell names a liberty file defines."""
    text = liberty.read_text(encoding="utf-8", errors="replace")
    return frozenset(re.findall(r'\bcell\s*\(\s*"?([A-Za-z0-9_$]+)"?\s*\)', text))


def _refuse_unsupported(netlist: dict[str, Any]) -> None:
    for module_name, module in netlist["modules"].items():
        for cell_name, cell in module.get("cells", {}).items():
            if cell.get("type") in _REFUSED_CELLS:
                raise InsertError(
                    f"{module_name}.{cell_name} is a {cell['type']} cell; mbist-insert "
                    "writes the design back with Yosys and can't keep it"
                )
            for bits in cell.get("connections", {}).values():
                if "z" in bits:
                    raise InsertError(
                        f"{module_name}.{cell_name} drives z: tri-state logic isn't "
                        "supported"
                    )


def _int(value: Any) -> int:
    text = str(value)
    return int(text, 2) if text and set(text) <= {"0", "1"} else int(text)


def _parameters(cell: dict[str, Any], module: dict[str, Any]) -> dict[str, int]:
    """A cell's effective parameter values: its module's defaults, overridden."""
    merged = dict(module.get("parameter_default_values", {}))
    merged.update(cell.get("parameters", {}))
    return {k: _int(v) for k, v in merged.items()}


def _without_attributes(value: Any) -> Any:
    """`value` with every "attributes" mapping dropped: two elaborations of the
    same source differ only in where Yosys says it came from."""
    if isinstance(value, dict):
        return {
            k: _without_attributes(v) for k, v in value.items() if k != "attributes"
        }
    if isinstance(value, list):
        return [_without_attributes(v) for v in value]
    return value


def _same_module(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if _is_blackbox(a) and _is_blackbox(b):
        ports = lambda m: {  # noqa: E731
            k: (p.get("direction"), len(p.get("bits", [])))
            for k, p in m.get("ports", {}).items()
        }
        return ports(a) == ports(b)
    return _without_attributes(a) == _without_attributes(b)


def _groups(spec: MbistSpec) -> list[_Group]:
    groups: dict[tuple[Path, str | None], _Group] = {}
    configs: dict[Path, CollarConfig] = {}
    for memory in spec.memories:
        if memory.autombist_config not in configs:
            configs[memory.autombist_config] = load_collar_config(
                memory.autombist_config
            )
        key = (memory.autombist_config, memory.algo)
        group = groups.setdefault(key, _Group(configs[key[0]], memory.algo, []))
        group.memories.append(memory)
    named: dict[str, _Group] = {}
    for group in groups.values():
        name = group.config.wrapper_module_name
        other = named.get(name)
        if other is not None:
            raise InsertError(
                f"two collars would both be named {name}: memories "
                f"{[m.name for m in other.memories]} (algo {other.algo or 'default'}) "
                f"and {[m.name for m in group.memories]} (algo "
                f"{group.algo or 'default'}) need different collars. Give them the "
                "same algo, or a config with its own wrapper_module_name"
            )
        named[name] = group
    return list(groups.values())


def _collar_sources(manifest_path: Path) -> tuple[str, list[Path]]:
    """The collar module autoMBIST generated, and its RTL (without the memory's
    blackbox stub: the design's own stub stands in for it)."""
    manifest = load_autombist_manifest(manifest_path)
    sources = [manifest.wrapper]
    for instance in manifest.instances:
        if instance.hierarchy_hint == "separate":
            sources.extend(p for p in instance.sources if p not in sources)
    return manifest.top_module, sources


def _elaborate_files(
    files: Sequence[Path], design: DesignSources, top: str, workdir: Path
) -> Modules:
    """`files` (autoMBIST and FaultFlow RTL) elaborated under `top`, with the
    design's macro stubs, defines and include directories."""
    netlist = elaborate(
        DesignSources(
            sources=tuple(files),
            libs=design.libs,
            include_dirs=design.include_dirs,
            defines=design.defines,
        ),
        top,
        workdir=workdir,
    ).netlist
    modules: Modules = netlist["modules"]
    return modules


def _sram(collar: dict[str, Any], config: CollarConfig) -> dict[str, Any]:
    cells = [
        cell
        for cell in collar.get("cells", {}).values()
        if cell.get("type") == config.memory_name
    ]
    if len(cells) != 1:
        raise InsertError(
            f"the collar has {len(cells)} {config.memory_name} instances, not one"
        )
    return cells[0]


def _check_sram(
    collar: dict[str, Any],
    config: CollarConfig,
    modules: Modules,
    instances: Sequence[tuple[MemorySpec, dict[str, Any]]],
) -> None:
    """The collar's macro instance is each memory's: same parameter values."""
    macro = modules[config.memory_name]
    collar_params = _parameters(_sram(collar, config), macro)
    for memory, cell in instances:
        params = _parameters(cell, macro)
        if params != collar_params:
            raise InsertError(
                f"memory {memory.name!r}: the collar instantiates "
                f"{config.memory_name} with {collar_params}, the design with {params}"
            )


def _drive_extra_pins(
    collar: dict[str, Any],
    config: CollarConfig,
    macro: dict[str, Any],
    memory: MemorySpec,
) -> None:
    """Drive the macro's pins outside the collar's roles inside the collar,
    as the design drove them: each tie to its constant, each share_clock pin
    from the macro's clock net."""
    sram = _sram(collar, config)
    conns = sram.setdefault("connections", {})
    directions = sram.setdefault("port_directions", {})
    for pin in [pin for pin, _ in memory.tie] + list(memory.share_clock):
        if conns.get(pin):
            raise InsertError(f"the collar already drives {config.memory_name}.{pin}")
    for pin, value in memory.tie:
        width = len(macro["ports"][pin]["bits"])
        conns[pin] = ["1" if value >> i & 1 else "0" for i in range(width)]
        directions[pin] = "input"
    for pin in memory.share_clock:
        conns[pin] = list(conns[config.pins["clk"]])
        directions[pin] = "input"


def _tie_key(memory: MemorySpec) -> tuple[Any, ...]:
    return (tuple(sorted(memory.tie)), tuple(sorted(memory.share_clock)))


def _build_shells(
    spec: MbistSpec,
    groups: Sequence[_Group],
    modules: Modules,
    macro_cells: dict[str, dict[str, Any]],
    work: Path,
) -> tuple[Modules, dict[str, tuple[str, tuple[CollarPort, ...]]]]:
    """The modules every shell needs, and each memory's shell and its ports."""
    library = work / "faultflow_mbist_library.v"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text(LIBRARY_VERILOG, encoding="utf-8")
    added: Modules = {}
    shells: dict[str, tuple[str, tuple[CollarPort, ...]]] = {}
    for k, group in enumerate(groups):
        gen = work / f"gen{k}"
        manifest_path = invoke_autombist_generate(
            group.config.path, gen, cmd=spec.autombist_cmd, algo=group.algo
        )
        collar_module, sources = _collar_sources(manifest_path)
        alone = _elaborate_files(sources, spec.design, collar_module, gen / "collar")
        ports = collar_ports(alone[collar_module])
        variants: dict[tuple[Any, ...], list[MemorySpec]] = {}
        for memory in group.memories:
            variants.setdefault(_tie_key(memory), []).append(memory)
        for v, members in enumerate(variants.values()):
            suffix = f"_v{v}" if len(variants) > 1 else ""
            shell_name = f"{collar_module}_shell{suffix}"
            shell_file = gen / f"{shell_name}.v"
            shell_file.write_text(
                shell_verilog(shell_name, collar_module, ports), encoding="utf-8"
            )
            built = _elaborate_files(
                [*sources, library, shell_file], spec.design, shell_name, gen / f"v{v}"
            )
            collar = built.pop(collar_module)
            _check_sram(
                collar,
                group.config,
                modules,
                [(m, macro_cells[m.name]) for m in members],
            )
            _drive_extra_pins(
                collar, group.config, modules[group.config.memory_name], members[0]
            )
            built[f"{collar_module}{suffix}"] = collar
            built[shell_name]["cells"]["u_collar"]["type"] = f"{collar_module}{suffix}"
            for name, module in built.items():
                if name in added and not _same_module(added[name], module):
                    raise InsertError(f"two different generated modules named {name}")
                added.setdefault(name, module)
            for memory in members:
                shells[memory.name] = (shell_name, ports)
    return added, shells


def _plain_names(added: Modules, taken: set[str]) -> Modules:
    """The generated modules, each Yosys-derived name (`$paramod$<hash>\\
    march_c_top`, `$paramod\\faultflow_mbist_sync2\\WIDTH=...`) replaced by a
    plain identifier, `<base>__<digest of the derived name>`: the same module
    gets the same name every time, and synthesis can name it in a script."""
    import hashlib

    from faultflow.mbist.netlist import _module_base

    renames: dict[str, str] = {}
    for name in added:
        if name.startswith("$"):
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
            plain = f"{_module_base(name)}__{digest}"
            if plain in taken or plain in added:
                raise InsertError(
                    f"the generated module {name} would be named {plain}, which is "
                    "taken"
                )
            renames[name] = plain
    for module in added.values():
        for cell in module.get("cells", {}).values():
            if cell.get("type") in renames:
                cell["type"] = renames[cell["type"]]
    return {renames.get(name, name): module for name, module in added.items()}


def mbist_insert(
    spec_path: str | Path,
    top: str,
    *,
    out: Path,
    taken: Iterable[str] = (),
) -> InsertResult:
    """Insert the collars of the insertion file's memories into the design
    under `top`, writing `<out>/<top>_mbist.v` and `<out>/insertion.json`.
    `taken` names cells (a liberty's, say) no module mbist-insert creates may
    be named after."""
    spec = load_mbist_spec(spec_path)
    if not spec.memories:
        raise InsertError("the insertion file configures no memory")
    if spec.reset is None:
        raise InsertError("mbist-insert needs the chip reset: reset: {port, active}")
    if spec.jtag:
        raise InsertError(
            "jtag: true isn't supported yet; with jtag: false the test ports "
            "become chip pins"
        )
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"

    groups = _groups(spec)
    design = elaborate(spec.design, top, workdir=work / "design")
    _refuse_unsupported(design.netlist)
    modules: Modules = design.netlist["modules"]

    # Every memory found and its pins verified before anything is generated.
    macro_cells: dict[str, dict[str, Any]] = {}
    for group in groups:
        if group.config.memory_name not in modules:
            raise InsertError(f"the design has no module {group.config.memory_name}")
        for memory in group.memories:
            parent, cell = locate(modules, top, memory.instance)[-1]
            verify_pins(modules, parent, cell, memory, group.config)
            macro_cells[memory.name] = modules[parent]["cells"][cell]

    added, shells = _build_shells(spec, groups, modules, macro_cells, work)
    taken_names = set(taken)
    added = _plain_names(added, set(modules) | taken_names)
    for name, module in added.items():
        if name in modules:
            if _same_module(modules[name], module):
                continue
            raise InsertError(
                f"the design already has a module named {name}, which mbist-insert "
                "needs for the inserted logic"
            )
        if name in taken_names:
            raise InsertError(f"{name} is a liberty cell name; mbist-insert needs it")
        modules[name] = module

    inserted: list[InsertedMemory] = []
    copies: dict[str, str] = {}
    for group in groups:
        for memory in group.memories:
            chain, made = uniquify_path(
                modules, top, locate(modules, top, memory.instance), taken_names
            )
            copies.update(made)
            shell_name, ports = shells[memory.name]
            threaded = swap_and_thread(
                modules,
                chain,
                memory,
                group.config,
                shell_name,
                ports,
                reset_port=spec.reset.port,
                reset_active_low=spec.reset.active_low,
            )
            inserted.append(
                InsertedMemory(
                    memory=memory.name,
                    instance=threaded.shell_path,
                    shell=shell_name,
                    collar=modules[shell_name]["cells"]["u_collar"]["type"],
                    top_ports=threaded.top_ports,
                )
            )

    merged = work / f"{top}_mbist.json"
    merged.write_text(json.dumps(design.netlist), encoding="utf-8")
    rtl = out / f"{top}_mbist.v"
    _run_yosys(
        [f"read_json {_quote(merged)}", f"write_verilog -noattr {_quote(rtl)}"],
        log_path=work / "write_verilog.log",
        script_path=work / "write_verilog.ys",
        error_message="Yosys could not write the inserted design",
    )
    _recheck(spec.design, rtl, top, design.problems, copies, work / "recheck")

    report = out / "insertion.json"
    report.write_text(
        json.dumps(
            {
                "top": top,
                "rtl": str(rtl),
                "memories": [
                    {
                        "name": m.memory,
                        "instance": m.instance,
                        "shell": m.shell,
                        "collar": m.collar,
                        "top_ports": m.top_ports,
                    }
                    for m in inserted
                ],
                "module_copies": copies,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return InsertResult(
        rtl=rtl, report=report, top=top, memories=tuple(inserted), copies=copies
    )


def ofs_liberty(ofs: Path) -> Path | None:
    """The liberty an .ofs names in [design] (resolved like config.py's paths,
    against the current directory), if any."""
    import configparser

    parser = configparser.ConfigParser()
    if not parser.read(ofs, encoding="utf-8"):
        raise InsertError(f"cannot read {ofs}")
    value = parser.get("design", "liberty", fallback="").strip()
    return Path(value) if value else None


def insert_command(
    spec: Path, top: str, out: Path | None, ofs: Path | None = None
) -> str:
    """ff.py mbist-insert / Tcl mbist_insert: run mbist_insert, and say what it
    did."""
    taken: frozenset[str] = frozenset()
    if ofs is not None:
        liberty = ofs_liberty(ofs)
        if liberty is not None:
            taken = liberty_cells(liberty)
    result = mbist_insert(spec, top, out=out or Path(f"mbist_{top}"), taken=taken)
    lines = [
        f"inserted {len(result.memories)} memories into {top}: {result.rtl}",
        f"report: {result.report}",
    ]
    for memory in result.memories:
        lines.append(
            f"  {memory.memory}: {memory.instance} ({memory.shell}); test ports "
            + ", ".join(memory.top_ports.values())
        )
    if result.copies:
        lines.append(
            "module copies: "
            + ", ".join(f"{c} (of {o})" for c, o in result.copies.items())
        )
    return "\n".join(lines)


def _recheck(
    design: DesignSources,
    rtl: Path,
    top: str,
    baseline: Sequence[CheckProblem],
    copies: dict[str, str],
    workdir: Path,
) -> None:
    """Elaborate the written design again; it may have no `check` problem the
    original didn't (a problem in a module copy counts as its original's)."""
    again = elaborate(
        DesignSources(
            sources=(rtl,),
            libs=design.libs,
            include_dirs=design.include_dirs,
            defines=design.defines,
        ),
        top,
        workdir=workdir,
    )
    known = {p.message for p in baseline}
    new = []
    for problem in again.problems:
        message = problem.message
        for copy_name, original in copies.items():
            message = message.replace(f"{copy_name}.", f"{original}.")
        if message not in known:
            new.append(problem.message)
    if new:
        raise InsertError(
            "the inserted design has problems the original didn't: "
            + "; ".join(new[:10])
            + f"; see {again.log_path}"
        )
