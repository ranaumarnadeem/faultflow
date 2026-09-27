"""FaultFlow synthesis generation from autoMBIST instance manifests.

autoMBIST (a separate tool, package ``autombist``) generates MBIST wrapper RTL
for SRAM macros and, with ``--emit-manifest``, an ``autombist_instance_manifest``
JSON describing which RTL instances are memory (always blackboxed, never
synthesized) versus test-instrument logic (``hierarchy_hint == "separate"``:
synthesizable standalone, needs ATPG grading). FaultFlow talks to autoMBIST only
as a subprocess (``invoke_autombist_generate``) and by reading the files it
writes -- never imports or modifies it.

The pipeline mirrors ``faultflow.project.assemble``'s own two-phase design
(freeze each block standalone, then splice into a glue netlist) but is NOT
``assemble_soc`` reused as-is: that function reads the glue without ``-sv``
(autoMBIST's wrapper is SystemVerilog), writes one stub per *instance* rather
than per distinct module (`assemble_soc`'s own callers never share a module
across instances, so this never mattered before), and ``block_stub_verilog``
emits no parameters (autoMBIST's "separate" instruments are frequently
parameterized, e.g. ``ADDR_WIDTH``/``DATA_WIDTH`` on a march algorithm). This
module writes its own parallel orchestration but reuses ``compose_soc`` (the
actual splicer) and ``block_stub_verilog`` (extended with an optional
``parameters`` argument) directly.

Every instance is keyed by its ``hierarchical_path`` (the name it will carry as
a flat cell after ``flatten``), never ``instance_name`` -- the two happen to be
textually identical for a flat (non-nested) design but are documented as
distinct fields, and only ``hierarchical_path`` survives synthesis as the real
netlist cell name.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError
from faultflow.project.assemble import AssembleError, block_stub_verilog, compose_soc

MANIFEST_FORMAT = "autombist_instance_manifest"


class AutombistManifestError(ConfigError):
    """A malformed or inconsistent autoMBIST instance manifest, or a
    plan_blocks grouping conflict (data-shape problems, not subprocess or
    synthesis failures)."""


class AutombistRunError(RuntimeError):
    """The `autombist generate` subprocess failed, or its output didn't match
    the documented <out>/<stem>/manifest.json contract (zero or >1 match)."""


@dataclass(frozen=True)
class AutombistInstance:
    category: str
    hierarchical_path: str
    hierarchy_hint: str  # "blackbox" | "separate"
    instance_name: str
    module_type: str
    sources: tuple[Path, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    present_because: str = "always"
    geometry: dict[str, Any] | None = None


@dataclass(frozen=True)
class AutombistManifest:
    top_module: str
    wrapper: Path
    instances: tuple[AutombistInstance, ...]
    # Captured for forward-compatibility with autoMBIST's JTAG-wrapped mode
    # (`autombist wrap-test-access`), never interpreted here -- that flow is a
    # separate, larger integration (new instance categories, a different glue
    # recipe) explicitly out of scope for this module. Its presence must not
    # break loading a manifest.
    test_access: Any | None = None
    root: Path = field(default=Path("."))


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise AutombistManifestError(f"{where}: missing required field {key!r}")
    return obj[key]


def _resolve(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AutombistManifestError(f"{where}: expected a non-empty path string")
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def load_autombist_manifest(path: str | Path) -> AutombistManifest:
    """Parse + validate an autoMBIST instance manifest.

    Every `sources` path resolves against the manifest FILE's own directory
    (`manifest_path.resolve().parent`), mirroring
    `faultflow.project.manifest.ProjectManifest.root`'s exact convention --
    deliberately never against the JSON's own `module_outdir` string, which is
    single-machine debris (an absolute path baked in wherever autoMBIST was
    originally run) and would make a manifest non-portable if trusted.
    """
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise AutombistManifestError(f"autoMBIST manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutombistManifestError(
            f"autoMBIST manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AutombistManifestError("autoMBIST manifest root must be a JSON object")

    fmt = data.get("format")
    if fmt != MANIFEST_FORMAT:
        raise AutombistManifestError(
            f"unsupported manifest format {fmt!r}; expected {MANIFEST_FORMAT!r}"
        )

    schema_version = str(_req(data, "schema_version", "manifest"))
    major = schema_version.split(".", 1)[0]
    if major != "1":
        raise AutombistManifestError(
            f"unsupported manifest schema_version {schema_version!r}; "
            "expected major version 1"
        )

    root = manifest_path.resolve().parent

    top_module = str(_req(data, "top_module", "manifest"))
    raw_sources = _req(data, "sources", "manifest")
    if not isinstance(raw_sources, dict):
        raise AutombistManifestError("manifest.sources must be an object")
    wrapper = _resolve(
        root, _req(raw_sources, "wrapper", "manifest.sources"), "manifest.sources"
    )

    raw_instances = _req(data, "instances", "manifest")
    if not isinstance(raw_instances, list) or not raw_instances:
        raise AutombistManifestError("manifest.instances must be a non-empty list")

    instances: list[AutombistInstance] = []
    seen_paths: set[str] = set()
    for i, raw in enumerate(raw_instances):
        where = f"manifest.instances[{i}]"
        if not isinstance(raw, dict):
            raise AutombistManifestError(f"{where}: must be an object")
        hierarchical_path = str(_req(raw, "hierarchical_path", where))
        if hierarchical_path in seen_paths:
            raise AutombistManifestError(
                f"{where}: duplicate hierarchical_path {hierarchical_path!r}"
            )
        seen_paths.add(hierarchical_path)

        hierarchy_hint = str(_req(raw, "hierarchy_hint", where))
        if hierarchy_hint not in ("blackbox", "separate"):
            raise AutombistManifestError(
                f"{where}: hierarchy_hint must be 'blackbox' or 'separate', "
                f"got {hierarchy_hint!r}"
            )

        raw_src_list = _req(raw, "sources", where)
        if not isinstance(raw_src_list, list) or not raw_src_list:
            raise AutombistManifestError(f"{where}: sources must be a non-empty list")
        sources = tuple(_resolve(root, s, where) for s in raw_src_list)

        parameters = raw.get("parameters", {})
        if not isinstance(parameters, dict):
            raise AutombistManifestError(f"{where}: parameters must be an object")

        geometry = raw.get("geometry")
        if geometry is not None and not isinstance(geometry, dict):
            raise AutombistManifestError(f"{where}: geometry must be an object")

        instances.append(
            AutombistInstance(
                category=str(_req(raw, "category", where)),
                hierarchical_path=hierarchical_path,
                hierarchy_hint=hierarchy_hint,
                instance_name=str(_req(raw, "instance_name", where)),
                module_type=str(_req(raw, "module_type", where)),
                sources=sources,
                parameters=dict(parameters),
                present_because=str(raw.get("present_because", "always")),
                geometry=dict(geometry) if geometry is not None else None,
            )
        )

    return AutombistManifest(
        top_module=top_module,
        wrapper=wrapper,
        instances=tuple(instances),
        test_access=data.get("test_access"),
        root=root,
    )


@dataclass(frozen=True)
class Block:
    """A distinct synthesizable unit: one or more "separate" instances that
    share the same (module_type, parameters) and therefore compile to the
    same gate-level netlist -- synthesized ONCE, spliced into every one of
    `instance_paths`' glue instance sites."""

    module: str
    parameters: dict[str, Any]
    sources: tuple[Path, ...]
    instance_paths: tuple[str, ...]


def plan_blocks(manifest: AutombistManifest) -> tuple[Block, ...]:
    """Group "separate" instances into Blocks by (module_type, parameters).

    "blackbox" instances (memories) never appear here -- they're never
    synthesized; their own already-blackbox-tagged source is reused directly
    during glue synthesis. Raises AutombistManifestError if one module_type
    appears with two different parameter sets (in the composed glue both
    would be the same cell type, so there'd be no way to tell them apart) or
    with different sources (a real manifest inconsistency, not something to
    silently resolve by picking one)."""
    by_module: dict[str, Block] = {}
    order: list[str] = []
    for inst in manifest.instances:
        if inst.hierarchy_hint != "separate":
            continue
        existing = by_module.get(inst.module_type)
        if existing is None:
            by_module[inst.module_type] = Block(
                module=inst.module_type,
                parameters=dict(inst.parameters),
                sources=inst.sources,
                instance_paths=(inst.hierarchical_path,),
            )
            order.append(inst.module_type)
            continue
        if existing.parameters != inst.parameters:
            raise AutombistManifestError(
                f"module_type {inst.module_type!r} appears with two different "
                f"parameter sets: {existing.parameters!r} vs {inst.parameters!r} "
                f"(instance {inst.hierarchical_path!r})"
            )
        if existing.sources != inst.sources:
            raise AutombistManifestError(
                f"module_type {inst.module_type!r} appears with two different "
                f"source lists: {existing.sources!r} vs {inst.sources!r} "
                f"(instance {inst.hierarchical_path!r})"
            )
        by_module[inst.module_type] = Block(
            module=existing.module,
            parameters=existing.parameters,
            sources=existing.sources,
            instance_paths=existing.instance_paths + (inst.hierarchical_path,),
        )
    return tuple(by_module[m] for m in order)


def invoke_autombist_generate(
    config: Path, out: Path, cmd: Sequence[str] = ("autombist",)
) -> Path:
    """Run `<cmd> generate --config <config> --out <out> --emit-manifest` and
    return the path to the single manifest.json it writes.

    `cmd` is a command PREFIX (e.g. `("autombist",)` or `("python3", "-m",
    "autombist.cli")`), not a single binary-name string -- the two forms are
    not interchangeable in every environment (the `autombist` console-script
    can be on PATH in an environment where `python -m autombist.cli` fails
    because the module isn't importable under whatever interpreter is
    running, or vice versa).
    """
    proc = subprocess.run(
        [
            *cmd,
            "generate",
            "--config",
            str(config),
            "--out",
            str(out),
            "--emit-manifest",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AutombistRunError(
            f"autombist generate failed (exit {proc.returncode}): {proc.stderr}"
        )
    matches = sorted(out.glob("*/manifest.json"))
    if not matches:
        raise AutombistRunError(
            f"autombist generate produced no */manifest.json under {out}"
        )
    if len(matches) > 1:
        raise AutombistRunError(
            f"autombist generate produced multiple manifest.json files under "
            f"{out}: {matches}"
        )
    return matches[0]


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_yosys(
    script_lines: list[str],
    *,
    log_path: Path,
    script_path: Path,
    error_message: str = "autoMBIST synthesis failed",
) -> None:
    yosys = shutil.which("yosys")
    if yosys is None:
        raise AssembleError("autoMBIST synthesis requires yosys on PATH")
    script_path.parent.mkdir(parents=True, exist_ok=True)
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
        raise AssembleError(f"{error_message}; see {log_path}")


def _check_composed_netlist_drivers(
    manifest: AutombistManifest,
    composed_json_path: Path,
    *,
    liberty: Path,
    workdir: Path,
) -> None:
    """Guard against a repeat of compose_soc's silent-connection-loss class of
    bug: run Yosys `check -assert` on the FINAL composed netlist and raise if
    it reports ANY "used but has no driver" or multiple-driver problem.

    Each blackbox (memory) instance's own real source is loaded via
    `read_verilog -lib` so `check` knows its port directions -- "separate"
    blocks need no such stub here, since by this point they're already real,
    fully-visible sky130 gates spliced into the composed JSON, not blackbox
    instance cells. autoMBIST's real glue netlists check clean (0 problems,
    measured -- autoMBIST ties unused block inputs to constants explicitly),
    so zero is the bar for the composed netlist too. A failure means either
    compose_soc dropped a connection, or the wrapper genuinely leaves a block
    input unconnected: compose_soc lets that input float (valid Verilog), and
    the spliced cells reading it then have no driver.
    """
    memory_sources = [
        inst.sources[0]
        for inst in manifest.instances
        if inst.hierarchy_hint == "blackbox"
    ]
    script_lines = [f"read_liberty -lib {_quote(liberty)}"]
    if memory_sources:
        script_lines.append(
            "read_verilog -lib " + " ".join(_quote(p) for p in memory_sources)
        )
    script_lines.extend(
        [
            f"read_json {_quote(composed_json_path)}",
            f"hierarchy -top {manifest.top_module}",
            "check -assert",
        ]
    )
    _run_yosys(
        script_lines,
        log_path=workdir / "composed_check.log",
        script_path=workdir / "composed_check.tcl",
        error_message=(
            "composed netlist has undriven or multiply-driven nets: either the "
            "wrapper leaves a block input unconnected (it floats), or "
            "compose_soc dropped a connection"
        ),
    )


def synthesize_block(block: Block, *, liberty: Path, workdir: Path) -> Path:
    """Synthesize one Block standalone, producing <workdir>/blocks/<module>.json.

    One Yosys run per Block (not per instance) -- `block.instance_paths` may
    have more than one entry, all sharing this single synthesis.
    """
    blocks_dir = workdir / "blocks"
    output_json = blocks_dir / f"{block.module}.json"
    log_path = blocks_dir / f"{block.module}_synth.log"
    script_path = blocks_dir / f"{block.module}_synth.tcl"

    reads = " ".join(_quote(p) for p in block.sources)
    script_lines = [f"read_verilog -sv {reads}"]
    if block.parameters:
        sets = " ".join(
            f"-set {k} {v!r}" if isinstance(v, str) else f"-set {k} {v}"
            for k, v in block.parameters.items()
        )
        script_lines.append(f"chparam {sets} {block.module}")
    script_lines.extend(
        [
            f"hierarchy -check -top {block.module}",
            "proc",
            "flatten",
            f"synth -top {block.module}",
            f"dfflibmap -liberty {_quote(liberty)}",
            f"abc -liberty {_quote(liberty)}",
            "delete t:$scopeinfo",
            "clean",
            f"write_json {_quote(output_json)}",
        ]
    )
    _run_yosys(script_lines, log_path=log_path, script_path=script_path)
    return output_json


def synthesize_glue(
    manifest: AutombistManifest,
    blocks: tuple[Block, ...],
    block_json_paths: dict[str, Path],
    *,
    liberty: Path,
    workdir: Path,
) -> Path:
    """Synthesize the wrapper glue (each block + each memory as a blackbox),
    producing <workdir>/glue.json. Verifies the glue top's non-library cell
    names equal the manifest's full hierarchical_path set -- a hard failure
    (AssembleError), not a warning, since a mismatch here means splicing would
    silently miss or misname an instance downstream.
    """
    stubs_dir = workdir / "stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    lib_sources: list[Path] = []
    for block in blocks:
        block_json = json.loads(
            block_json_paths[block.module].read_text(encoding="utf-8")
        )
        stub = block_stub_verilog(block_json, block.module, parameters=block.parameters)
        stub_path = stubs_dir / f"{block.module}_stub.v"
        stub_path.write_text(stub, encoding="utf-8")
        lib_sources.append(stub_path)
    memory_paths = {
        inst.hierarchical_path: inst.sources[0]
        for inst in manifest.instances
        if inst.hierarchy_hint == "blackbox"
    }
    lib_sources.extend(memory_paths.values())

    output_json = workdir / "glue.json"
    log_path = workdir / "glue_synth.log"
    script_path = workdir / "glue_synth.tcl"

    script_lines = [f"read_verilog -sv {_quote(manifest.wrapper)}"]
    if lib_sources:
        script_lines.append(
            "read_verilog -lib " + " ".join(_quote(p) for p in lib_sources)
        )
    script_lines.extend(
        [
            f"hierarchy -check -top {manifest.top_module}",
            "proc",
            "flatten",
            f"synth -top {manifest.top_module}",
            f"dfflibmap -liberty {_quote(liberty)}",
            f"abc -liberty {_quote(liberty)}",
            "delete t:$scopeinfo",
            "clean",
            f"write_json {_quote(output_json)}",
        ]
    )
    _run_yosys(script_lines, log_path=log_path, script_path=script_path)

    glue_json = json.loads(output_json.read_text(encoding="utf-8"))
    modules = glue_json.get("modules", {})
    top = modules.get(manifest.top_module)
    if not isinstance(top, dict):
        raise AssembleError(
            f"glue synthesis did not produce top module {manifest.top_module!r}"
        )
    # Verify every manifest instance survived synthesis as its own untouched
    # instance cell, named exactly its hierarchical_path and typed exactly its
    # module_type -- NOT that these are the ONLY cells (the wrapper may have
    # real interconnect logic of its own, e.g. test-mode muxing, which
    # legitimately synthesizes into real library gates alongside the
    # blackbox instances).
    cells = top.get("cells", {})
    missing: list[str] = []
    wrong_type: list[str] = []
    for inst in manifest.instances:
        cell = cells.get(inst.hierarchical_path)
        if not isinstance(cell, dict):
            missing.append(inst.hierarchical_path)
            continue
        if str(cell.get("type", "")) != inst.module_type:
            wrong_type.append(inst.hierarchical_path)
    if missing or wrong_type:
        raise AssembleError(
            "glue synthesis lost or renamed manifest instances: "
            f"missing={missing}, wrong_type={wrong_type}"
        )
    return output_json


def write_ofs(
    path: Path,
    *,
    netlist: Path,
    top: str,
    cell_lib: Path,
    liberty: Path,
    blackbox_instances: tuple[str, ...],
) -> Path:
    """Write a fresh `.ofs` for the composed netlist. `.ofs` paths resolve
    against the CWD, not the .ofs file's own directory (`config.py::_path`) --
    every path here is written absolute (`.resolve()`d) to be safe regardless
    of the caller's cwd. Deliberately no `top =` line: no `.ofs` in this
    codebase carries one, `top` is always a separate CLI/API argument.
    """
    lines = [
        "[design]",
        f"netlist = {netlist.resolve()}",
        f"cell_lib = {cell_lib.resolve()}",
        f"liberty = {liberty.resolve()}",
        "",
        "[blackbox]",
        f"instances = {', '.join(blackbox_instances)}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@dataclass(frozen=True)
class AutombistSynthesisResult:
    ofs_path: Path
    composed_json_path: Path
    top_module: str
    blackbox_instances: tuple[str, ...]
    instance_counts: dict[str, int]
    block_count: int


def synthesize_from_manifest(
    manifest: AutombistManifest,
    *,
    out: Path,
    liberty: Path,
    cell_lib: Path,
) -> AutombistSynthesisResult:
    """Steps 2-6: plan_blocks -> synthesize each block -> synthesize glue ->
    compose_soc -> write .ofs. No autoMBIST subprocess invocation -- this is
    the entry point for callers that already have a manifest (e.g. the
    fixture-based integration test)."""
    workdir = out / "autombist_synth"
    blocks = plan_blocks(manifest)

    block_json_paths = {
        block.module: synthesize_block(block, liberty=liberty, workdir=workdir)
        for block in blocks
    }
    glue_json_path = synthesize_glue(
        manifest, blocks, block_json_paths, liberty=liberty, workdir=workdir
    )
    glue_json = json.loads(glue_json_path.read_text(encoding="utf-8"))

    blocks_by_instance: dict[str, dict[str, Any]] = {}
    module_by_instance: dict[str, str] = {}
    for block in blocks:
        block_json = json.loads(
            block_json_paths[block.module].read_text(encoding="utf-8")
        )
        for hp in block.instance_paths:
            blocks_by_instance[hp] = block_json
            module_by_instance[hp] = block.module

    composed = compose_soc(
        glue_json=glue_json,
        soc_top=manifest.top_module,
        blocks=blocks_by_instance,
        block_module=module_by_instance,
    )
    composed_json_path = out / f"{manifest.top_module}_composed.json"
    composed_json_path.parent.mkdir(parents=True, exist_ok=True)
    composed_json_path.write_text(json.dumps(composed, indent=2), encoding="utf-8")

    _check_composed_netlist_drivers(
        manifest, composed_json_path, liberty=liberty, workdir=workdir
    )

    blackbox_instances = tuple(
        inst.hierarchical_path
        for inst in manifest.instances
        if inst.hierarchy_hint == "blackbox"
    )
    ofs_path = out / f"{manifest.top_module}.ofs"
    write_ofs(
        ofs_path,
        netlist=composed_json_path,
        top=manifest.top_module,
        cell_lib=cell_lib,
        liberty=liberty,
        blackbox_instances=blackbox_instances,
    )

    instance_counts: dict[str, int] = {}
    for inst in manifest.instances:
        instance_counts[inst.category] = instance_counts.get(inst.category, 0) + 1

    return AutombistSynthesisResult(
        ofs_path=ofs_path,
        composed_json_path=composed_json_path,
        top_module=manifest.top_module,
        blackbox_instances=blackbox_instances,
        instance_counts=instance_counts,
        block_count=len(blocks),
    )


def run_autombist_generate(
    config: Path,
    out: Path,
    *,
    autombist_cmd: Sequence[str] = ("autombist",),
    liberty: Path,
    cell_lib: Path,
) -> AutombistSynthesisResult:
    """Steps 1-6 in full: invoke autoMBIST -> load its manifest ->
    synthesize_from_manifest. This is what the CLI and Tcl handlers call."""
    manifest_path = invoke_autombist_generate(config, out, cmd=autombist_cmd)
    manifest = load_autombist_manifest(manifest_path)
    return synthesize_from_manifest(
        manifest, out=out, liberty=liberty, cell_lib=cell_lib
    )
