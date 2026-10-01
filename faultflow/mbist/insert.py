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
7. With jtag, put the test ports behind a TAP and an IJTAG network (jtag.py)
   and remove them: the chip gains the TAP's five pins only.
8. Write the design back with Yosys, and elaborate it again: `check` may report
   no problem the original design didn't have.
9. Write the SDC of the crossings inserted, the manifest (read back to check
   it; with JTAG, its network rebuilt too) and, with JTAG, the ICL and BSDL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.integrations.autombist import (
    AutombistManifestError,
    CheckProblem,
    _quote,
    _run_yosys,
    invoke_autombist_generate,
    load_autombist_manifest,
)
from faultflow.mbist.autombist_config import CollarConfig, load_collar_config
from faultflow.mbist.jtag import (
    TAP_PORTS,
    WARPTAP_MODULES,
    JtagInsertion,
    TestPort,
    check_names,
    insert_network,
    write_descriptions,
)
from faultflow.mbist.manifest import ShellRecord, chip_manifest, macro_sources
from faultflow.mbist.netlist import (
    InsertError,
    _is_blackbox,
    locate,
    swap_and_thread,
    uniquify_path,
    verify_pins,
)
from faultflow.mbist.program import (
    ProgramMemory,
    clock_port,
    render_pdl,
    render_vectors,
    retarget,
    run_steps,
)
from faultflow.mbist.sdc import crossings, rtl_sdc
from faultflow.mbist.shell import (
    LIBRARY_VERILOG,
    CollarPort,
    collar_ports,
    shell_verilog,
)
from faultflow.mbist.spec import DesignSources, MbistSpec, MemorySpec, load_mbist_spec
from faultflow.mbist.synth import shell_leaves
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
    # collar port -> its top port; with JTAG, the instrument of that name (the
    # port itself is gone, its net keeps the name)
    top_ports: dict[str, str]


@dataclass(frozen=True)
class InsertResult:
    rtl: Path
    report: Path
    top: str
    memories: tuple[InsertedMemory, ...]
    copies: dict[str, str] = field(default_factory=dict)  # copy -> original
    manifest: Path | None = None
    sdc: Path | None = None
    # With JTAG:
    jtag: JtagInsertion | None = None
    icl: Path | None = None
    bsdl: Path | None = None
    pdl: Path | None = None  # the BIST program
    vectors: Path | None = None  # ... as TCK and clock vectors
    program_memories: tuple[ProgramMemory, ...] = ()
    schedule: tuple[tuple[str, ...], ...] = ()


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


_SRC = re.compile(r"^(.*):(\d+)\.\d+-(\d+)\.\d+$")


def _source_text(module: dict[str, Any]) -> str | None:
    """The source text a module was elaborated from (its src attribute: a file
    and a line range), or None when that can't be read."""
    found = _SRC.match(str(module.get("attributes", {}).get("src", "")))
    if found is None:
        return None
    try:
        lines = Path(found.group(1)).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return "\n".join(lines[int(found.group(2)) - 1 : int(found.group(3))])


def _same_module(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if _is_blackbox(a) and _is_blackbox(b):
        ports = lambda m: {  # noqa: E731
            k: (p.get("direction"), len(p.get("bits", [])))
            for k, p in m.get("ports", {}).items()
        }
        return ports(a) == ports(b)
    if _without_attributes(a) == _without_attributes(b):
        return True
    # Elaborated in two runs -- FaultFlow's library for two collars, or a module
    # two autoMBIST runs both generate -- one module differs only in the names
    # Yosys makes up. The same source text with the same parameters is the same.
    text = _source_text(a)
    return (
        text is not None
        and text == _source_text(b)
        and a.get("parameter_default_values") == b.get("parameter_default_values")
    )


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


@dataclass(frozen=True)
class _Shell:
    module: str
    ports: tuple[CollarPort, ...]
    # autoMBIST's category of each instance in the collar, and the memory's.
    categories: dict[str, str]
    memory_instance: str
    # autoMBIST's bound on the BIST's length in clk cycles, if it states one.
    bist_bound: int | None = None


_MAX_CYCLES = re.compile(r"`define\s+MBIST_MAX_CYCLES\s+(\d+)")


def _bist_bound(generated: Path, collar_module: str) -> int | None:
    """autoMBIST's bound on the collar's BIST length, in clk cycles: what its
    generated testbench (tb/tb_<collar>.sv) times out at, MBIST_MAX_CYCLES."""
    testbench = generated / "tb" / f"tb_{collar_module}.sv"
    if not testbench.is_file():
        return None
    found = _MAX_CYCLES.search(testbench.read_text(encoding="utf-8", errors="replace"))
    return int(found.group(1)) if found else None


def _collar_sources(manifest_path: Path) -> tuple[str, list[Path], dict[str, str], str]:
    """The collar module autoMBIST generated, its RTL (without the memory's
    blackbox stub: the design's own stub stands in for it), the category of
    each instance in it, and the memory's instance."""
    manifest = load_autombist_manifest(manifest_path)
    sources = [manifest.wrapper]
    for instance in manifest.instances:
        if instance.hierarchy_hint == "separate":
            sources.extend(p for p in instance.sources if p not in sources)
    categories = {i.hierarchical_path: i.category for i in manifest.instances}
    memories = [
        i.hierarchical_path for i in manifest.instances if i.category == "memory"
    ]
    if len(memories) != 1:
        raise InsertError(
            f"autoMBIST's manifest {manifest_path} lists {len(memories)} memories, "
            "not one"
        )
    return manifest.top_module, sources, categories, memories[0]


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
) -> tuple[Modules, dict[str, _Shell]]:
    """The modules every shell needs, and each memory's shell."""
    library = work / "faultflow_mbist_library.v"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text(LIBRARY_VERILOG, encoding="utf-8")
    added: Modules = {}
    shells: dict[str, _Shell] = {}
    for k, group in enumerate(groups):
        gen = work / f"gen{k}"
        manifest_path = invoke_autombist_generate(
            group.config.path, gen, cmd=spec.autombist_cmd, algo=group.algo
        )
        collar_module, sources, categories, memory_instance = _collar_sources(
            manifest_path
        )
        bound = _bist_bound(manifest_path.parent, collar_module)
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
                shells[memory.name] = _Shell(
                    shell_name, ports, categories, memory_instance, bound
                )
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
    under `top`, writing into `out`: `<top>_mbist.v`, `<top>_mbist.sdc`,
    `manifest.json`, `insertion.json` and, with JTAG, `<top>_mbist.icl` and
    `<top>_mbist.bsd`. `taken` names cells (a liberty's, say) no module
    mbist-insert creates may be named after."""
    spec = load_mbist_spec(spec_path)
    if not spec.memories:
        raise InsertError("the insertion file configures no memory")
    if spec.reset is None:
        raise InsertError("mbist-insert needs the chip reset: reset: {port, active}")
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"

    groups = _groups(spec)
    design = elaborate(spec.design, top, workdir=work / "design")
    _refuse_unsupported(design.netlist)
    modules: Modules = design.netlist["modules"]
    taken_names = set(taken)
    if spec.jtag is not None:
        check_names(modules, top, taken_names)
        # No module mbist-insert names may take a name the network needs.
        taken_names |= set(WARPTAP_MODULES)

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
            raise InsertError(
                f"{name} is a liberty cell's or the JTAG network's name; mbist-insert "
                "needs it"
            )
        modules[name] = module

    inserted: list[InsertedMemory] = []
    copies: dict[str, str] = {}
    records: list[ShellRecord] = []
    sdc_shells: list[tuple[tuple[str, ...], tuple[CollarPort, ...]]] = []
    test_ports: list[TestPort] = []
    program_memories: list[ProgramMemory] = []
    for group in groups:
        for memory in group.memories:
            chain, made = uniquify_path(
                modules, top, locate(modules, top, memory.instance), taken_names
            )
            copies.update(made)
            shell = shells[memory.name]
            threaded = swap_and_thread(
                modules,
                chain,
                memory,
                group.config,
                shell.module,
                shell.ports,
                reset_port=spec.reset.port,
                reset_active_low=spec.reset.active_low,
            )
            collar = modules[shell.module]["cells"]["u_collar"]["type"]
            inserted.append(
                InsertedMemory(
                    memory=memory.name,
                    instance=threaded.shell_path,
                    shell=shell.module,
                    collar=collar,
                    top_ports=threaded.top_ports,
                )
            )
            records.append(
                ShellRecord(
                    path=threaded.shell_path,
                    leaves=shell_leaves(modules, shell.module, collar),
                    collar_categories=shell.categories,
                    memory_instance=shell.memory_instance,
                    macro=group.config.memory_name,
                    macro_sources=macro_sources(
                        group.config.memory_name,
                        [*spec.design.libs, *spec.design.sources],
                    ),
                )
            )
            sdc_shells.append((tuple(cell for _, cell in chain), shell.ports))
            for port in shell.ports:
                if port.kind in ("control", "status", "done"):
                    role = "control" if port.kind == "control" else "status"
                    test_ports.append(
                        TestPort(
                            threaded.top_ports[port.name],
                            role,
                            port.width,
                            capture_sync=role == "status",
                        )
                    )
            if spec.jtag is not None:
                parent, cell_name = chain[-1]
                clk = modules[parent]["cells"][cell_name]["connections"]["clk"]
                program_memories.append(
                    _program_memory(
                        memory.name,
                        threaded.top_ports,
                        clock_port(modules, chain, clk),
                        shell.bist_bound,
                    )
                )

    jtag: JtagInsertion | None = None
    if spec.jtag is not None:
        jtag = insert_network(
            design.netlist, top, test_ports, reset=spec.reset, jtag=spec.jtag
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

    icl = bsdl = pdl = vectors = None
    if jtag is not None and spec.jtag is not None:
        icl, bsdl = write_descriptions(jtag, spec.jtag, top, out)
        pdl, vectors = write_program(
            jtag, program_memories, spec.schedule, top, out, icl=icl, bsdl=bsdl
        )
    sdc = out / f"{top}_mbist.sdc"
    sdc.write_text(rtl_sdc(rtl.name, crossings(sdc_shells, jtag)), encoding="utf-8")
    manifest = out / "manifest.json"
    data = chip_manifest(
        top,
        rtl,
        records,
        root=out,
        jtag=jtag,
        top_cells=modules[top].get("cells", {}),
        reset=spec.reset,
        icl=icl,
        bsdl=bsdl,
    )
    manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _read_back(manifest, jtag)

    report = out / "insertion.json"
    summary: dict[str, Any] = {
        "top": top,
        "rtl": str(rtl),
        "sdc": str(sdc),
        "manifest": str(manifest),
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
    }
    if jtag is not None:
        summary["jtag"] = {
            "icl": str(icl),
            "bsdl": str(bsdl),
            "bist_pdl": str(pdl),
            "bist_vectors": str(vectors),
            "network_instruction": "IJTAG_ACCESS",
            "network_opcode": jtag.opcode,
            "idcode": jtag.idcode,
            "instruments": [p.name for p in jtag.ports],
        }
    report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return InsertResult(
        rtl=rtl,
        report=report,
        top=top,
        memories=tuple(inserted),
        copies=copies,
        manifest=manifest,
        sdc=sdc,
        jtag=jtag,
        icl=icl,
        bsdl=bsdl,
        pdl=pdl,
        vectors=vectors,
        program_memories=tuple(program_memories),
        schedule=spec.schedule,
    )


def _program_memory(
    name: str, instruments: dict[str, str], clock: str | None, bound: int | None
) -> ProgramMemory:
    if clock is None:
        raise InsertError(
            f"memory {name!r}: its clock comes from logic, not a chip input, so the "
            "BIST program has no clock to run (iRunLoop -sck)"
        )
    if bound is None:
        raise InsertError(
            f"memory {name!r}: autoMBIST's testbench (tb/tb_<collar>.sv) states no "
            "bound on the BIST's length (MBIST_MAX_CYCLES), which the BIST program "
            "runs the clock for"
        )
    return ProgramMemory(name, dict(instruments), clock, bound)


def write_program(
    jtag: JtagInsertion,
    memories: Sequence[ProgramMemory],
    schedule: Sequence[Sequence[str]],
    top: str,
    out: Path,
    *,
    icl: Path | None = None,
    bsdl: Path | None = None,
) -> tuple[Path, Path]:
    """`<out>/<top>_run_mbist.pdl` and `<top>_run_mbist.vec`: the BIST program
    for `memories` over the inserted network, and its vectors."""
    steps = run_steps(memories, schedule)
    vectors = retarget(jtag.graph, jtag.root, steps, opcode=jtag.opcode)
    clocks = sorted({m.clock for m in memories})
    pdl = out / f"{top}_run_mbist.pdl"
    vec = out / f"{top}_run_mbist.vec"
    pdl.write_text(
        render_pdl(
            top,
            steps,
            schedule=schedule,
            widths={p.name: p.width for p in jtag.ports},
            opcode=jtag.opcode,
            clocks=clocks,
            icl=None if icl is None else icl.name,
            bsdl=None if bsdl is None else bsdl.name,
            vectors=vec.name,
        ),
        encoding="utf-8",
    )
    vec.write_text(render_vectors(vectors, clocks), encoding="utf-8")
    return pdl, vec


def _read_back(manifest: Path, jtag: JtagInsertion | None) -> None:
    """Read the manifest written as FaultFlow will; with JTAG, rebuild its
    network, which must be the one inserted."""
    try:
        loaded = load_autombist_manifest(manifest)
    except AutombistManifestError as exc:
        raise InsertError(f"the manifest written can't be read back: {exc}") from exc
    if jtag is None:
        return
    from faultflow.integrations.autombist_jtag import (
        AutombistJtagError,
        rebuild_network,
    )

    if loaded.test_access is None:
        raise InsertError("the manifest written has no test_access block")
    try:
        graph, _root = rebuild_network(loaded.test_access)
    except AutombistJtagError as exc:
        raise InsertError(
            f"the manifest's network isn't the one inserted: {exc}"
        ) from exc
    if graph != jtag.graph:
        raise InsertError(
            "the network rebuilt from the manifest isn't the one inserted"
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
        f"manifest: {result.manifest}",
        f"SDC of the crossings inserted: {result.sdc}",
        f"report: {result.report}",
    ]
    kind = "test ports" if result.jtag is None else "instruments"
    for memory in result.memories:
        lines.append(
            f"  {memory.memory}: {memory.instance} ({memory.shell}); {kind} "
            + ", ".join(memory.top_ports.values())
        )
    if result.jtag is not None:
        lines.append(
            f"JTAG: a TAP on {', '.join(TAP_PORTS)}; IJTAG_ACCESS "
            f"({result.jtag.opcode:04b}) selects the network; ICL {result.icl}, "
            f"BSDL {result.bsdl}"
        )
        lines.append(f"BIST program: {result.pdl} (vectors {result.vectors})")
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
