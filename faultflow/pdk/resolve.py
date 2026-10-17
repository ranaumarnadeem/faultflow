"""A standard-cell library by name, from a PDK in the open_pdks layout:
``<root>/<pdk>/libs.ref/<library>/lib/<library>__<corner>.lib`` and
``.../verilog/{primitives.v,<library>.v}``. Sky130 (sky130A, sky130B), GF180MCU and IHP
SG13G2 ship this way; ciel installs them (``ciel fetch --pdk-family sky130 -l all``).

The cell map is generated from the Liberty (faultflow.pdk.cell_map_gen) and cached by
the Liberty's hash, as is one Verilog models file -- the library's primitives and
cells, for iverilog. ``dont_use`` names every cell the map does not model, so
synthesis never emits one."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from faultflow.pdk.roles import PdkError

# Bump when the generator's output changes, so a cached map is made again.
GENERATOR_VERSION = "1"
# The nominal corners of the PDKs in this layout, tried first when none is named:
# Sky130's 1.8 V cells and its 3.3 V hvl, GF180MCU's 5 V and 3.3 V, IHP SG13G2's.
NOMINAL_CORNERS = (
    "tt_025C_1v80",
    "tt_025C_3v30",
    "tt_025C_5v00",
    "typ_1p20V_25C",
)


@dataclass(frozen=True)
class ResolvedLibrary:
    pdk: str
    library: str
    liberty: Path
    verilog_models: Path
    cell_map: Path
    dont_use: tuple[str, ...]


def pdk_root(explicit: Path | str | None) -> Path:
    """The PDK root: ``explicit``, else ``$PDK_ROOT``, else ciel's ``~/.ciel``."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PDK_ROOT")
    return Path(env) if env else Path.home() / ".ciel"


def cache_dir(explicit: Path | str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "faultflow" / "pdk"


def _corner(lib_dir: Path, library: str, corner: str | None) -> Path:
    if corner:
        path = lib_dir / f"{library}__{corner}.lib"
        if not path.exists():
            have = sorted(
                p.stem.split("__", 1)[-1] for p in lib_dir.glob(f"{library}__*.lib")
            )
            raise PdkError(
                f"{library} has no corner {corner} (it has: {', '.join(have)})"
            )
        return path
    found = sorted(lib_dir.glob(f"{library}__*.lib"))
    if not found:
        raise PdkError(f"{lib_dir} has no Liberty file for {library}")
    for nominal in NOMINAL_CORNERS:
        path = lib_dir / f"{library}__{nominal}.lib"
        if path.exists():
            return path
    # Else a typical corner at 25 C, else any typical, else the first.
    for prefer in ("tt_025C_", "tt_"):
        typical = [p for p in found if p.stem.split("__", 1)[-1].startswith(prefer)]
        if typical:
            return typical[0]
    return found[0]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def resolve_library(
    pdk: str,
    library: str,
    *,
    corner: str | None = None,
    root: Path | str | None = None,
    cache: Path | str | None = None,
) -> ResolvedLibrary:
    base = pdk_root(root) / pdk / "libs.ref" / library
    if not (base / "lib").is_dir():
        raise PdkError(
            f"the PDK root {pdk_root(root)} has no {pdk} library {library}: install it "
            f"(ciel fetch --pdk-family {pdk.rstrip('AB')} -l all <version>, then ciel "
            "enable) or set [design] pdk_root"
        )
    liberty = _corner(base / "lib", library, corner)
    stem = f"{library}-{_sha(liberty)}-g{GENERATOR_VERSION}"
    out = cache_dir(cache)
    out.mkdir(parents=True, exist_ok=True)
    cell_map = out / f"{stem}.json"
    report = out / f"{stem}.report.json"
    if not (cell_map.exists() and report.exists()):
        from faultflow.pdk.command import cell_map_command

        tmp_map, tmp_report = cell_map.with_suffix(".tmp"), report.with_suffix(".tmp")
        cell_map_command(liberty, tmp_map, report=tmp_report)
        tmp_report.replace(report)
        tmp_map.replace(cell_map)
    models = out / f"{library}-{pdk}-models.v"
    verilog = base / "verilog"
    sources = [verilog / "primitives.v", verilog / f"{library}.v"]
    if not sources[1].exists():
        raise PdkError(f"{verilog} has no {library}.v (the cells' Verilog models)")
    text = "".join(
        f"// {src}\n" + src.read_text(encoding="utf-8", errors="replace") + "\n"
        for src in sources
        if src.exists()
    )
    if (
        not models.exists()
        or models.read_text(encoding="utf-8", errors="replace") != text
    ):
        models.write_text(text, encoding="utf-8")
    listed = json.loads(report.read_text(encoding="utf-8"))
    dont_use = tuple(sorted({str(r["cell"]) for r in listed}))
    return ResolvedLibrary(pdk, library, liberty, models, cell_map, dont_use)
