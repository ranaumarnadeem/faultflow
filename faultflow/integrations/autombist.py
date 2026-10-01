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

A design wrapped for JTAG access (``autombist wrap-test-access``, recorded in
the manifest's ``test_access`` block) is built from that block alone: its
``output_verilog`` defines every module the wrapped netlist uses, already
parameter-specialized (``$paramod$<hash>\\march_c_top``), plus warptap's TAP,
SIB and TDR cells. Each distinct "separate" module is synthesized once from
that file, the glue is the same file with those modules blackboxed, and the
memory stays the base manifest's blackbox stub. The TAP and its IJTAG network
run on tck/trst_n, the MBIST logic on clk/rst_n: a two-clock-domain design --
or, with ``tap_nonscan``, one: the .ofs keeps the TAP and the network out of
scan ([scan] nonscan_cells) and holds them in reset ([scan] hold), and ff.py
jtag grades them through TCK instead.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError, JtagConfig
from faultflow.project.assemble import AssembleError, block_stub_verilog, compose_soc
from faultflow.scan.stitch import scan_clock_domains

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
class AutombistTestAccessInstance:
    """One instance of the JTAG-wrapped netlist (a `test_access.instances`
    entry). `module_type` is spelled as the wrapped netlist spells it, e.g.
    `\\$paramod$<hash>\\march_c_top`: used verbatim, never re-parameterized.
    `sources` is set for a blackbox (the memory) only: its stub. SIB and TDR
    instances name their SIB (`sib_name`), and a TDR bit its `instrument` and
    `bit`."""

    category: str
    hierarchical_path: str
    hierarchy_hint: str  # "blackbox" | "separate"
    instance_name: str
    module_type: str
    sources: tuple[Path, ...] = ()
    sib_name: str | None = None
    instrument: str | None = None
    bit: int | None = None


@dataclass(frozen=True)
class AutombistTestAccessInstrument:
    """One wrapped port: a control port becomes JTAG-only, a status port stays
    readable at its pin and is only tapped. Listed in scan-chain order, TDI
    side first; `sib` and `tdr_bits` are instance paths."""

    name: str
    role: str  # "control" | "status"
    width: int
    sib: str
    tdr_bits: tuple[str, ...]


@dataclass(frozen=True)
class AutombistTestAccess:
    """The manifest's `test_access` block for a design wrapped with a JTAG TAP
    and an IJTAG network (`autombist wrap-test-access`)."""

    top_module: str
    output_verilog: Path
    boundary_ports: tuple[str, ...]
    instances: tuple[AutombistTestAccessInstance, ...]
    instruments: tuple[AutombistTestAccessInstrument, ...]
    icl_path: Path | None = None


@dataclass(frozen=True)
class AutombistManifest:
    top_module: str
    wrapper: Path
    instances: tuple[AutombistInstance, ...]
    # Set when `autombist wrap-test-access` wrapped the design for JTAG
    # access: synthesis then builds from this block instead of `instances`.
    test_access: AutombistTestAccess | None = None
    root: Path = field(default=Path("."))
    # The manifest file this was loaded from.
    path: Path | None = None


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise AutombistManifestError(f"{where}: missing required field {key!r}")
    return obj[key]


def _resolve(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AutombistManifestError(f"{where}: expected a non-empty path string")
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return None if value is None else str(value)


def _load_test_access(
    raw: Any, root: Path, base: tuple[AutombistInstance, ...]
) -> AutombistTestAccess | None:
    """Parse the `test_access` block, or None for a design not wrapped for
    JTAG access. Paths resolve like `sources`: absolute as written (autoMBIST
    writes them so), relative against the manifest's own directory. A memory
    entry without its own `sources` takes the base instance's stub."""
    if raw is None:
        return None
    where = "manifest.test_access"
    if not isinstance(raw, dict):
        raise AutombistManifestError(f"{where} must be an object or null")
    if raw.get("wrapped") is not True:
        return None
    if raw.get("memory_blackboxed") is not True:
        raise AutombistManifestError(
            f"{where}.memory_blackboxed is not true: the wrapped netlist builds the "
            "memory from its model, but FaultFlow tests the memory as a blackbox. "
            "Rerun `autombist wrap-test-access --manifest DIR`, which wraps the "
            "memory's blackbox stub"
        )
    top_module = str(_req(raw, "top_module", where))
    output_verilog = _resolve(
        root, _req(raw, "output_verilog", where), f"{where}.output_verilog"
    )
    icl_raw = raw.get("icl_path")
    icl_path = None if icl_raw is None else _resolve(root, icl_raw, f"{where}.icl")
    ports = _req(raw, "boundary_ports", where)
    if not isinstance(ports, list) or not all(isinstance(p, str) for p in ports):
        raise AutombistManifestError(f"{where}.boundary_ports must list port names")

    base_sources = {inst.hierarchical_path: inst.sources for inst in base}
    raw_instances = _req(raw, "instances", where)
    if not isinstance(raw_instances, list) or not raw_instances:
        raise AutombistManifestError(f"{where}.instances must be a non-empty list")
    instances: list[AutombistTestAccessInstance] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_instances):
        at = f"{where}.instances[{i}]"
        if not isinstance(entry, dict):
            raise AutombistManifestError(f"{at}: must be an object")
        path = str(_req(entry, "hierarchical_path", at))
        if path in seen:
            raise AutombistManifestError(f"{at}: duplicate hierarchical_path {path!r}")
        seen.add(path)
        hint = str(_req(entry, "hierarchy_hint", at))
        if hint not in ("blackbox", "separate"):
            raise AutombistManifestError(
                f"{at}: hierarchy_hint must be 'blackbox' or 'separate', got {hint!r}"
            )
        sources: tuple[Path, ...] = ()
        if hint == "blackbox":
            raw_sources = entry.get("sources")
            if raw_sources is None:
                sources = base_sources.get(path, ())
            elif isinstance(raw_sources, list):
                sources = tuple(_resolve(root, s, at) for s in raw_sources)
            if not sources:
                raise AutombistManifestError(
                    f"{at}: blackbox {path!r} has no stub: neither its own sources "
                    "nor a base instance of that path"
                )
        bit = entry.get("bit")
        if bit is not None and not isinstance(bit, int):
            raise AutombistManifestError(f"{at}: bit must be an integer")
        instances.append(
            AutombistTestAccessInstance(
                category=str(_req(entry, "category", at)),
                hierarchical_path=path,
                hierarchy_hint=hint,
                instance_name=str(entry.get("instance_name", path)),
                module_type=str(_req(entry, "module_type", at)),
                sources=sources,
                sib_name=_optional_str(entry, "sib_name"),
                instrument=_optional_str(entry, "instrument"),
                bit=bit,
            )
        )

    raw_instruments = _req(raw, "instruments", where)
    if not isinstance(raw_instruments, list):
        raise AutombistManifestError(f"{where}.instruments must be a list")
    instruments: list[AutombistTestAccessInstrument] = []
    for i, entry in enumerate(raw_instruments):
        at = f"{where}.instruments[{i}]"
        if not isinstance(entry, dict):
            raise AutombistManifestError(f"{at}: must be an object")
        role = str(_req(entry, "role", at))
        if role not in ("control", "status"):
            raise AutombistManifestError(
                f"{at}: role must be 'control' or 'status', got {role!r}"
            )
        width = _req(entry, "width", at)
        if not isinstance(width, int) or width < 1:
            raise AutombistManifestError(f"{at}: width must be a positive integer")
        sib = str(_req(entry, "sib", at))
        tdr_bits = _req(entry, "tdr_bits", at)
        if not isinstance(tdr_bits, list) or len(tdr_bits) != width:
            raise AutombistManifestError(
                f"{at}: tdr_bits must list one TDR cell per bit ({width})"
            )
        unknown = [p for p in (sib, *map(str, tdr_bits)) if p not in seen]
        if unknown:
            raise AutombistManifestError(
                f"{at}: names instances not in {where}.instances: {unknown}"
            )
        instruments.append(
            AutombistTestAccessInstrument(
                name=str(_req(entry, "name", at)),
                role=role,
                width=width,
                sib=sib,
                tdr_bits=tuple(str(b) for b in tdr_bits),
            )
        )

    return AutombistTestAccess(
        top_module=top_module,
        output_verilog=output_verilog,
        boundary_ports=tuple(ports),
        instances=tuple(instances),
        instruments=tuple(instruments),
        icl_path=icl_path,
    )


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
        test_access=_load_test_access(data.get("test_access"), root, tuple(instances)),
        root=root,
        path=manifest_path.resolve(),
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
    config: Path,
    out: Path,
    cmd: Sequence[str] = ("autombist",),
    *,
    algo: str | None = None,
) -> Path:
    """Run `<cmd> generate --config <config> --out <out> --emit-manifest` and
    return the path to the single manifest.json it writes. `algo` adds
    `--algo <algo>` (march-c, march-raw, ...); without it autoMBIST uses its
    default.

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
            *(["--algo", algo] if algo is not None else []),
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


def invoke_autombist_wrap_test_access(
    module_dir: Path, cmd: Sequence[str] = ("autombist",)
) -> None:
    """Run `<cmd> wrap-test-access --manifest <module_dir> --emit-icl`, which
    wraps the generated design's control/status ports with a JTAG TAP and an
    IJTAG network and records the result in manifest.json's `test_access`
    block. autoMBIST needs the warptap package importable, plus Yosys and
    Icarus Verilog on PATH."""
    proc = subprocess.run(
        [*cmd, "wrap-test-access", "--manifest", str(module_dir), "--emit-icl"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AutombistRunError(
            f"autombist wrap-test-access failed (exit {proc.returncode}): "
            f"{proc.stderr or proc.stdout}"
        )


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _synth_lines(top: str, liberty: Path, output_json: Path) -> list[str]:
    """The locked synthesis sequence, from `hierarchy` to `write_json`, that
    every block and glue run shares once its sources are read."""
    return [
        f"hierarchy -check -top {top}",
        "proc",
        "flatten",
        f"synth -top {top}",
        f"dfflibmap -liberty {_quote(liberty)}",
        f"abc -liberty {_quote(liberty)}",
        "delete t:$scopeinfo",
        "clean",
        f"write_json {_quote(output_json)}",
    ]


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


@dataclass(frozen=True)
class CheckProblem:
    """One problem Yosys's `check` pass reported: its warning line, without
    "Warning: ", and the indented lines under it (a net's drivers, a loop's
    cells). The message names the net by module and wire, so it is the same
    from one synthesis run to the next; the details name cells, which aren't."""

    message: str
    details: tuple[str, ...] = ()


_CHECK_PASS = re.compile(r"^\d+(?:\.\d+)*\. Executing CHECK pass\b")
_CHECK_DONE = re.compile(r"^Found and reported (\d+) problems?\.")


def parse_check_problems(log: str) -> tuple[CheckProblem, ...]:
    """The problems the CHECK pass in a Yosys log reported, in order. Raises
    AssembleError if the log has no finished CHECK pass, or if the count Yosys
    printed isn't the number of warnings parsed -- a format this parser doesn't
    know, which must not pass as fewer problems."""
    lines = log.splitlines()
    start = next((i for i, line in enumerate(lines) if _CHECK_PASS.match(line)), None)
    if start is None:
        raise AssembleError("the Yosys log has no CHECK pass")
    problems: list[CheckProblem] = []
    message: str | None = None
    details: list[str] = []
    for line in lines[start + 1 :]:
        if message is not None and line.startswith((" ", "\t")):
            details.append(line.strip())
            continue
        if message is not None:
            problems.append(CheckProblem(message, tuple(details)))
            message, details = None, []
        done = _CHECK_DONE.match(line)
        if done:
            if int(done.group(1)) != len(problems):
                raise AssembleError(
                    f"Yosys reported {done.group(1)} check problems, but "
                    f"{len(problems)} were parsed from its log"
                )
            return tuple(problems)
        if line.startswith("Warning: "):
            message = line[len("Warning: ") :].strip()
    raise AssembleError("the Yosys CHECK pass in the log did not finish")


def _check_composed_netlist_drivers(
    manifest: AutombistManifest,
    composed_json_path: Path,
    *,
    liberty: Path,
    workdir: Path,
    baseline: frozenset[str] = frozenset(),
) -> None:
    """Guard against a repeat of compose_soc's silent-connection-loss class of
    bug: run Yosys `check` on the FINAL composed netlist and raise if it
    reports ANY problem -- a "used but has no driver" or multiple-driver net,
    say -- other than those in `baseline`: the CheckProblem messages of
    problems the design already had before composition, which composition
    isn't to blame for.

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
    access = manifest.test_access
    if access is not None:
        top = access.top_module
        memory_sources = _memory_stubs(access)
    else:
        top = manifest.top_module
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
            f"hierarchy -top {top}",
            "check",
        ]
    )
    log_path = workdir / "composed_check.log"
    _run_yosys(
        script_lines,
        log_path=log_path,
        script_path=workdir / "composed_check.tcl",
        error_message="the driver check of the composed netlist failed to run",
    )
    problems = parse_check_problems(log_path.read_text(encoding="utf-8"))
    new = [p for p in problems if p.message not in baseline]
    if new:
        shown = "; ".join(p.message for p in new[:10])
        more = f" (and {len(new) - 10} more)" if len(new) > 10 else ""
        raise AssembleError(
            f"composed netlist fails Yosys check ({len(new)} new problems): "
            "either the wrapper leaves a block input unconnected (it floats), or "
            f"compose_soc dropped a connection: {shown}{more}; see {log_path}"
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
    script_lines.extend(_synth_lines(block.module, liberty, output_json))
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
    script_lines.extend(_synth_lines(manifest.top_module, liberty, output_json))
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


def wrapped_block_modules(access: AutombistTestAccess) -> tuple[str, ...]:
    """The distinct module types of a wrapped design's "separate" instances, in
    manifest order: each is synthesized once and spliced into every instance
    of it (one SIB module serves every SIB)."""
    modules: list[str] = []
    for inst in access.instances:
        if inst.hierarchy_hint == "separate" and inst.module_type not in modules:
            modules.append(inst.module_type)
    return tuple(modules)


def _memory_stubs(access: AutombistTestAccess) -> list[Path]:
    stubs: list[Path] = []
    for inst in access.instances:
        if inst.hierarchy_hint != "blackbox":
            continue
        for source in inst.sources:
            if source not in stubs:
                stubs.append(source)
    return stubs


def _file_stems(modules: Sequence[str]) -> dict[str, str]:
    """A distinct file name stem per module. A wrapped design's module names
    carry Yosys's parameter mangling (`\\$paramod$<hash>\\march_c_top`)."""
    stems: dict[str, str] = {}
    for module in modules:
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", module).strip("_") or "module"
        stem, n = base, 1
        while stem in stems.values():
            n += 1
            stem = f"{base}_{n}"
        stems[module] = stem
    return stems


def _wrapped_reads(access: AutombistTestAccess) -> list[str]:
    lines = [f"read_verilog -sv {_quote(access.output_verilog)}"]
    stubs = _memory_stubs(access)
    if stubs:
        lines.append("read_verilog -lib " + " ".join(_quote(p) for p in stubs))
    return lines


def synthesize_wrapped_block(
    access: AutombistTestAccess,
    module: str,
    *,
    liberty: Path,
    workdir: Path,
    stem: str,
) -> Path:
    """Synthesize one module of the wrapped netlist standalone, from
    `output_verilog` as it stands, producing <workdir>/blocks/<stem>.json."""
    blocks_dir = workdir / "blocks"
    output_json = blocks_dir / f"{stem}.json"
    script_lines = _wrapped_reads(access) + _synth_lines(module, liberty, output_json)
    _run_yosys(
        script_lines,
        log_path=blocks_dir / f"{stem}_synth.log",
        script_path=blocks_dir / f"{stem}_synth.tcl",
    )
    return output_json


def synthesize_wrapped_glue(
    access: AutombistTestAccess,
    modules: Sequence[str],
    *,
    liberty: Path,
    workdir: Path,
) -> Path:
    """Synthesize the wrapped top with every module in `modules` blackboxed,
    producing <workdir>/glue.json, and check that its non-library cells are
    exactly the `test_access.instances`: a cell anywhere else would be logic
    no instance's category accounts for."""
    output_json = workdir / "glue.json"
    script_lines = [
        *_wrapped_reads(access),
        *(f"blackbox {module}" for module in modules),
        *_synth_lines(access.top_module, liberty, output_json),
    ]
    _run_yosys(
        script_lines,
        log_path=workdir / "glue_synth.log",
        script_path=workdir / "glue_synth.tcl",
    )
    glue_json = json.loads(output_json.read_text(encoding="utf-8"))
    modules_json = glue_json.get("modules", {})
    top = modules_json.get(access.top_module)
    if not isinstance(top, dict):
        raise AssembleError(
            f"glue synthesis did not produce top module {access.top_module!r}"
        )
    # A library cell's type is no module of the netlist; a blackbox's is, and
    # an unmapped Yosys cell's starts with "$".
    designs = set(modules_json) - {access.top_module}
    found: dict[str, str] = {}
    for name, cell in top.get("cells", {}).items():
        cell_type = str(cell.get("type", "")) if isinstance(cell, dict) else ""
        if cell_type in designs or cell_type.startswith("$"):
            found[name] = cell_type
    expected = {inst.hierarchical_path: inst.module_type for inst in access.instances}
    if found != expected:
        wrong = sorted(p for p in set(found) & set(expected) if found[p] != expected[p])
        raise AssembleError(
            "wrapped glue's non-library cells differ from test_access.instances: "
            f"missing={sorted(set(expected) - set(found))}, "
            f"unlisted={sorted(set(found) - set(expected))}, wrong_type={wrong}"
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
    clock_ports: tuple[str, ...] = (),
    scan_chains: int | None = None,
    manifest: Path | None = None,
    nonscan_cells: tuple[str, ...] = (),
    scan_holds: tuple[tuple[str, int], ...] = (),
) -> Path:
    """Write a fresh `.ofs` for the composed netlist. `.ofs` paths resolve
    against the CWD, not the .ofs file's own directory (`config.py::_path`) --
    every path here is written absolute (`.resolve()`d) to be safe regardless
    of the caller's cwd. Deliberately no `top =` line: no `.ofs` in this
    codebase carries one, `top` is always a separate CLI/API argument.

    `clock_ports` are declared in `[clocks]`, and `scan_chains`,
    `nonscan_cells` and `scan_holds` in `[scan]`. `manifest`, the autoMBIST
    manifest the netlist was built from, goes in `[autombist]`: the coverage
    report breaks coverage down by its instance categories.
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
    if clock_ports:
        lines.extend(["[clocks]", f"ports = {', '.join(clock_ports)}", ""])
    scan = [f"chains = {scan_chains}"] if scan_chains is not None else []
    if nonscan_cells:
        scan.append(f"nonscan_cells = {', '.join(nonscan_cells)}")
    if scan_holds:
        scan.append("hold = " + ", ".join(f"{port}:{v}" for port, v in scan_holds))
    if scan:
        lines.extend(["[scan]", *scan, ""])
    if manifest is not None:
        lines.extend(["[autombist]", f"manifest = {manifest.resolve()}", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _clock_domains(
    netlist: Path, top: str, cell_lib: Path, nonscan_cells: Sequence[str] = ()
) -> tuple[tuple[str, ...], int]:
    """The input ports clocking the netlist's scan flops -- all but those
    `nonscan_cells` names -- in clock-net order, and how many clock domains
    those flops form. A domain clocked from inside the design has no port to
    name; scan insertion reports it."""
    domains = scan_clock_domains(netlist, cell_lib, top, nonscan_cells)
    ports = json.loads(netlist.read_text(encoding="utf-8"))["modules"][top]["ports"]
    by_net = {
        port["bits"][0]: name
        for name, port in ports.items()
        if port.get("direction") == "input" and len(port.get("bits", [])) == 1
    }
    names = tuple(by_net[net] for net in sorted(domains) if net in by_net)
    return names, max(1, len(domains))


def literal_glob(text: str) -> str:
    """`text` as an fnmatch pattern that matches only itself. An instance path
    from a generate block, `u_core.g[0].u_mem`, would otherwise read `[0]` as a
    character set: it would match `u_core.g0.u_mem` and miss itself."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def tap_nonscan_settings(
    manifest: AutombistManifest,
) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
    """[scan] nonscan_cells and [scan] hold that run a JTAG-wrapped design's TAP
    and IJTAG network non-scan: a glob per JTAG-category instance, whose cells the
    composed netlist names `<instance path>__<cell>` (the path matched
    literally), and the TAP held in reset with its clock off -- trst_n clears
    every one of their flops."""
    from faultflow.integrations.autombist_coverage import (
        JTAG_CATEGORIES,
        instance_categories,
    )

    if manifest.test_access is None:
        raise AutombistManifestError(
            "a TAP runs non-scan only in a design wrapped for JTAG access "
            "(test_access)"
        )
    globs = tuple(
        sorted(
            f"{literal_glob(path)}__*"
            for path, category in instance_categories(manifest).items()
            if category in JTAG_CATEGORIES
        )
    )
    tap = JtagConfig()
    return globs, ((tap.trst_n, 0), (tap.tck, 0))


@dataclass(frozen=True)
class AutombistSynthesisResult:
    ofs_path: Path
    composed_json_path: Path
    top_module: str
    blackbox_instances: tuple[str, ...]
    instance_counts: dict[str, int]
    block_count: int
    # A design wrapped for JTAG access: the clock port of each clock domain,
    # and the scan chain count the .ofs asks for (one per domain).
    clock_ports: tuple[str, ...] = ()
    scan_chains: int | None = None
    # The manifest the design was built from, named in the .ofs [autombist].
    manifest_path: Path | None = None
    # With the TAP non-scan: the .ofs [scan] nonscan_cells and hold.
    nonscan_cells: tuple[str, ...] = ()
    scan_holds: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class _Composition:
    top: str
    netlist: dict[str, Any]
    blackbox_instances: tuple[str, ...]
    categories: tuple[str, ...]  # one per instance
    block_count: int


def _compose_base(
    manifest: AutombistManifest, *, liberty: Path, workdir: Path
) -> _Composition:
    """The design as `autombist generate` wrote it: blocks from `instances`."""
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
    return _Composition(
        top=manifest.top_module,
        netlist=composed,
        blackbox_instances=tuple(
            inst.hierarchical_path
            for inst in manifest.instances
            if inst.hierarchy_hint == "blackbox"
        ),
        categories=tuple(inst.category for inst in manifest.instances),
        block_count=len(blocks),
    )


def _compose_wrapped(
    access: AutombistTestAccess, *, liberty: Path, workdir: Path
) -> _Composition:
    """The JTAG-wrapped design, built from its `test_access` block alone."""
    if not access.output_verilog.is_file():
        raise AutombistManifestError(
            f"test_access.output_verilog not found: {access.output_verilog}"
        )
    modules = wrapped_block_modules(access)
    stems = _file_stems(modules)
    block_json = {
        module: json.loads(
            synthesize_wrapped_block(
                access, module, liberty=liberty, workdir=workdir, stem=stems[module]
            ).read_text(encoding="utf-8")
        )
        for module in modules
    }
    glue_json_path = synthesize_wrapped_glue(
        access, modules, liberty=liberty, workdir=workdir
    )
    separate = [i for i in access.instances if i.hierarchy_hint == "separate"]
    composed = compose_soc(
        glue_json=json.loads(glue_json_path.read_text(encoding="utf-8")),
        soc_top=access.top_module,
        blocks={i.hierarchical_path: block_json[i.module_type] for i in separate},
        block_module={i.hierarchical_path: i.module_type for i in separate},
    )
    return _Composition(
        top=access.top_module,
        netlist=composed,
        blackbox_instances=tuple(
            i.hierarchical_path
            for i in access.instances
            if i.hierarchy_hint == "blackbox"
        ),
        categories=tuple(i.category for i in access.instances),
        block_count=len(modules),
    )


def synthesize_from_manifest(
    manifest: AutombistManifest,
    *,
    out: Path,
    liberty: Path,
    cell_lib: Path,
    tap_nonscan: bool = False,
) -> AutombistSynthesisResult:
    """Steps 2-6: plan_blocks -> synthesize each block -> synthesize glue ->
    compose_soc -> write .ofs. No autoMBIST subprocess invocation -- this is
    the entry point for callers that already have a manifest (e.g. the
    fixture-based integration test). A design wrapped for JTAG access is built
    from its `test_access` block instead of `instances`; `tap_nonscan` writes
    its .ofs to run the TAP and the IJTAG network non-scan
    (tap_nonscan_settings)."""
    nonscan_cells: tuple[str, ...] = ()
    scan_holds: tuple[tuple[str, int], ...] = ()
    if tap_nonscan:
        nonscan_cells, scan_holds = tap_nonscan_settings(manifest)
    workdir = out / "autombist_synth"
    if manifest.test_access is not None:
        composition = _compose_wrapped(
            manifest.test_access, liberty=liberty, workdir=workdir
        )
    else:
        composition = _compose_base(manifest, liberty=liberty, workdir=workdir)

    composed_json_path = out / f"{composition.top}_composed.json"
    composed_json_path.parent.mkdir(parents=True, exist_ok=True)
    composed_json_path.write_text(
        json.dumps(composition.netlist, indent=2), encoding="utf-8"
    )

    _check_composed_netlist_drivers(
        manifest, composed_json_path, liberty=liberty, workdir=workdir
    )

    clock_ports: tuple[str, ...] = ()
    scan_chains: int | None = None
    if manifest.test_access is not None:
        # The TAP runs on its own clock, so the design has two clock domains
        # at least, and a scan chain never spans two: one chain per domain.
        # Non-scan, the TAP's clock clocks no scan flop: one domain.
        clock_ports, scan_chains = _clock_domains(
            composed_json_path, composition.top, cell_lib, nonscan_cells
        )
    ofs_path = out / f"{composition.top}.ofs"
    write_ofs(
        ofs_path,
        netlist=composed_json_path,
        top=composition.top,
        cell_lib=cell_lib,
        liberty=liberty,
        blackbox_instances=composition.blackbox_instances,
        clock_ports=clock_ports,
        scan_chains=scan_chains,
        manifest=manifest.path,
        nonscan_cells=nonscan_cells,
        scan_holds=scan_holds,
    )

    instance_counts: dict[str, int] = {}
    for category in composition.categories:
        instance_counts[category] = instance_counts.get(category, 0) + 1

    return AutombistSynthesisResult(
        ofs_path=ofs_path,
        composed_json_path=composed_json_path,
        top_module=composition.top,
        blackbox_instances=composition.blackbox_instances,
        instance_counts=instance_counts,
        block_count=composition.block_count,
        clock_ports=clock_ports,
        scan_chains=scan_chains,
        manifest_path=manifest.path,
        nonscan_cells=nonscan_cells,
        scan_holds=scan_holds,
    )


def run_autombist_generate(
    config: Path,
    out: Path,
    *,
    autombist_cmd: Sequence[str] = ("autombist",),
    liberty: Path,
    cell_lib: Path,
    test_access: bool = False,
    tap_nonscan: bool = False,
    algo: str | None = None,
) -> AutombistSynthesisResult:
    """Steps 1-6 in full: invoke autoMBIST -> load its manifest ->
    synthesize_from_manifest. This is what the CLI and Tcl handlers call.
    With `test_access`, autoMBIST first wraps the generated design for JTAG
    access, and the wrapped design is what gets synthesized; `tap_nonscan`
    then runs its TAP and IJTAG network non-scan. `algo` picks autoMBIST's
    MBIST algorithm (its default without it)."""
    if tap_nonscan and not test_access:
        raise ConfigError("the TAP runs non-scan only with test access")
    manifest_path = invoke_autombist_generate(config, out, cmd=autombist_cmd, algo=algo)
    if test_access:
        invoke_autombist_wrap_test_access(manifest_path.parent, cmd=autombist_cmd)
    manifest = load_autombist_manifest(manifest_path)
    if test_access and manifest.test_access is None:
        raise AutombistRunError(
            f"autombist wrap-test-access recorded no wrapped test_access block in "
            f"{manifest_path}"
        )
    return synthesize_from_manifest(
        manifest,
        out=out,
        liberty=liberty,
        cell_lib=cell_lib,
        tap_nonscan=tap_nonscan,
    )
