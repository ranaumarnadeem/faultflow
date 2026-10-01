"""The MBIST insertion file: the design's sources, and the memories to wrap.

```yaml
design:
  sources: [rtl/chip.v, rtl/core.sv]       # read_verilog -sv
  libs: [macros/sram_stubs.v]              # macro stubs, read_verilog -lib
  include_dirs: [rtl/include]
  defines: [SYNTHESIS]
reset: {port: rst_n, active: low}
jtag: true
autombist_cmd: autombist
memory_patterns: ["*sram*"]                # memory macro types list-memories lists
schedule: [[core0_ram], [top_ram]]         # steps run in order; default one per step
memories:
  - name: core0_ram                        # becomes part of port names
    instance: u_core0.u_mem                # as list-memories prints it
    autombist_config: mbist/sram.yml
    algo: march-c
    tie: {wmask0: 0xF, csb1: 1, addr1: 0}  # verified against the design's drivers
    share_clock: [clk1]                    # on the same net as the memory's clock
    unused_outputs: [dout1]                # read by nothing
```

Paths are relative to the insertion file. A JSON file needs nothing extra; a YAML
file needs PyYAML.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOP_KEYS = (
    "design",
    "reset",
    "jtag",
    "autombist_cmd",
    "memory_patterns",
    "schedule",
    "memories",
)
_DESIGN_KEYS = ("sources", "libs", "include_dirs", "defines")
_MEMORY_KEYS = (
    "name",
    "instance",
    "autombist_config",
    "algo",
    "tie",
    "share_clock",
    "unused_outputs",
)


class MbistSpecError(ConfigError):
    """The insertion file can't be read, or says something that can't be right."""


@dataclass(frozen=True)
class DesignSources:
    sources: tuple[Path, ...]
    libs: tuple[Path, ...] = ()
    include_dirs: tuple[Path, ...] = ()
    defines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResetSpec:
    """The chip reset input: the collars, the shell synchronizers and the
    control TDRs reset from it."""

    port: str
    active_low: bool


@dataclass(frozen=True)
class MemorySpec:
    name: str
    instance: str
    autombist_config: Path
    algo: str | None = None
    # Pins outside the collar's roles, each verified against the design.
    tie: tuple[tuple[str, int], ...] = ()
    share_clock: tuple[str, ...] = ()
    unused_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MbistSpec:
    path: Path
    design: DesignSources
    reset: ResetSpec | None = None
    jtag: bool = False
    autombist_cmd: tuple[str, ...] = ("autombist",)
    memory_patterns: tuple[str, ...] = ("*",)
    memories: tuple[MemorySpec, ...] = ()
    # Steps run one after another; a step's memories run together.
    schedule: tuple[tuple[str, ...], ...] = ()

    def memory_at(self, instance: str) -> MemorySpec | None:
        return next((m for m in self.memories if m.instance == instance), None)


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MbistSpecError(f"cannot read the insertion file {path}: {exc}") from exc
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MbistSpecError(f"{path}: not valid JSON: {exc}") from exc
    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:
            raise MbistSpecError(
                f"{path}: a YAML insertion file needs PyYAML (pip install pyyaml); "
                "a .json one needs nothing extra"
            ) from exc
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MbistSpecError(f"{path}: not valid YAML: {exc}") from exc
    raise MbistSpecError(f"{path}: an insertion file is .yml, .yaml or .json")


def _mapping(value: Any, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MbistSpecError(f"{where} must be a mapping")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise MbistSpecError(
            f"{where}: unknown key(s) {unknown}; allowed: {', '.join(keys)}"
        )
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MbistSpecError(f"{where} must be a list of strings")
    return tuple(value)


def _paths(
    value: Any, where: str, root: Path, *, directory: bool = False
) -> tuple[Path, ...]:
    paths = []
    for text in _strings(value, where):
        path = (root / text).resolve()
        if not (path.is_dir() if directory else path.is_file()):
            kind = "directory" if directory else "file"
            raise MbistSpecError(f"{where}: {kind} not found: {path}")
        paths.append(path)
    return tuple(paths)


def _design(raw: Any, root: Path) -> DesignSources:
    design = _mapping(raw, "design", _DESIGN_KEYS)
    sources = _paths(design.get("sources"), "design.sources", root)
    if not sources:
        raise MbistSpecError("design.sources must name at least one file")
    return DesignSources(
        sources=sources,
        libs=_paths(design.get("libs"), "design.libs", root),
        include_dirs=_paths(
            design.get("include_dirs"), "design.include_dirs", root, directory=True
        ),
        defines=_strings(design.get("defines"), "design.defines"),
    )


def _reset(raw: Any) -> ResetSpec | None:
    if raw is None:
        return None
    reset = _mapping(raw, "reset", ("port", "active"))
    port, active = reset.get("port"), reset.get("active")
    if not isinstance(port, str) or not port:
        raise MbistSpecError("reset.port must name the chip's reset input")
    if active not in ("low", "high"):
        raise MbistSpecError(f"reset.active must be 'low' or 'high', got {active!r}")
    return ResetSpec(port=port, active_low=active == "low")


def _tie_value(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise MbistSpecError(f"{where} must be an integer, got {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value, 0)
        except ValueError:
            raise MbistSpecError(
                f"{where} must be an integer (e.g. 15, 0xF, 0b1111), got {value!r}"
            ) from None
    else:
        raise MbistSpecError(f"{where} must be an integer, got {value!r}")
    if number < 0:
        raise MbistSpecError(f"{where} must not be negative, got {number}")
    return number


def _memory(raw: Any, index: int, root: Path) -> MemorySpec:
    where = f"memories[{index}]"
    memory = _mapping(raw, where, _MEMORY_KEYS)
    name = memory.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise MbistSpecError(
            f"{where}.name must be an identifier (letters, digits, underscores; "
            f"not starting with a digit), got {name!r}: it becomes part of port names"
        )
    where = f"memory {name!r}"
    instance = memory.get("instance")
    if not isinstance(instance, str) or not instance:
        raise MbistSpecError(f"{where}: instance must be the memory's instance path")
    config = memory.get("autombist_config")
    if not isinstance(config, str):
        raise MbistSpecError(
            f"{where}: autombist_config must name its autoMBIST config"
        )
    (config_path,) = _paths([config], f"{where}: autombist_config", root)
    algo = memory.get("algo")
    if algo is not None and not isinstance(algo, str):
        raise MbistSpecError(f"{where}: algo must be a string, got {algo!r}")
    tie_raw = memory.get("tie") or {}
    if not isinstance(tie_raw, dict) or not all(isinstance(k, str) for k in tie_raw):
        raise MbistSpecError(f"{where}: tie must map pin names to values")
    tie = tuple(
        (pin, _tie_value(value, f"{where}: tie.{pin}"))
        for pin, value in tie_raw.items()
    )
    share_clock = _strings(memory.get("share_clock"), f"{where}: share_clock")
    unused = _strings(memory.get("unused_outputs"), f"{where}: unused_outputs")
    pins = [pin for pin, _ in tie] + list(share_clock) + list(unused)
    twice = sorted({pin for pin in pins if pins.count(pin) > 1})
    if twice:
        raise MbistSpecError(
            f"{where}: pin(s) {twice} listed more than once across tie, share_clock "
            "and unused_outputs"
        )
    return MemorySpec(
        name=name,
        instance=instance,
        autombist_config=config_path,
        algo=algo,
        tie=tie,
        share_clock=share_clock,
        unused_outputs=unused,
    )


def _schedule(raw: Any, names: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return tuple((name,) for name in names)
    if raw == "concurrent":
        return (names,) if names else ()
    if not isinstance(raw, list) or not all(
        isinstance(step, list) and step and all(isinstance(n, str) for n in step)
        for step in raw
    ):
        raise MbistSpecError(
            "schedule must be 'concurrent' or a list of steps, each a non-empty "
            "list of memory names"
        )
    listed = [name for step in raw for name in step]
    unknown = sorted(set(listed) - set(names))
    twice = sorted({name for name in listed if listed.count(name) > 1})
    missing = [name for name in names if name not in listed]
    if unknown or twice or missing:
        raise MbistSpecError(
            "schedule must list every memory exactly once"
            + (f"; unknown: {unknown}" if unknown else "")
            + (f"; listed twice: {twice}" if twice else "")
            + (f"; missing: {missing}" if missing else "")
        )
    return tuple(tuple(step) for step in raw)


def load_mbist_spec(path: str | Path) -> MbistSpec:
    """Read and check an insertion file. Every path must exist; every memory
    needs a unique name and instance."""
    spec_path = Path(path).resolve()
    data = _mapping(_read(spec_path), str(spec_path), _TOP_KEYS)
    root = spec_path.parent
    if "design" not in data:
        raise MbistSpecError(f"{spec_path}: design is required")
    jtag = data.get("jtag", False)
    if not isinstance(jtag, bool):
        raise MbistSpecError(f"jtag must be true or false, got {jtag!r}")
    cmd = data.get("autombist_cmd", "autombist")
    if not isinstance(cmd, str) or not shlex.split(cmd):
        raise MbistSpecError("autombist_cmd must be a command, e.g. 'autombist'")
    patterns = _strings(data.get("memory_patterns"), "memory_patterns") or ("*",)
    raw_memories = data.get("memories") or []
    if not isinstance(raw_memories, list):
        raise MbistSpecError("memories must be a list")
    memories = tuple(_memory(raw, i, root) for i, raw in enumerate(raw_memories))
    names = tuple(m.name for m in memories)
    for label, values in (
        ("name", names),
        ("instance", [m.instance for m in memories]),
    ):
        twice = sorted({v for v in values if list(values).count(v) > 1})
        if twice:
            raise MbistSpecError(f"two memories share the {label} {twice[0]!r}")
    return MbistSpec(
        path=spec_path,
        design=_design(data["design"], root),
        reset=_reset(data.get("reset")),
        jtag=jtag,
        autombist_cmd=tuple(shlex.split(cmd)),
        memory_patterns=patterns,
        memories=memories,
        schedule=_schedule(data.get("schedule"), names),
    )
