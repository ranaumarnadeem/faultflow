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
import re
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


def ciel_family(pdk: str) -> str:
    """The ciel PDK family a PDK variant installs with: sky130A -> sky130,
    gf180mcuD -> gf180mcu, ihp-sg13g2 -> ihp-sg13."""
    for family, variant in (
        ("sky130", r"sky130[A-Z]"),
        ("gf180mcu", r"gf180mcu[A-Z]"),
        ("ihp-sg13", r"ihp-sg13\w+"),
    ):
        if re.fullmatch(variant, pdk):
            return family
    return pdk


def _corners(lib_dir: Path, library: str) -> dict[str, Path]:
    """A library's Liberty files by corner: ``<library>__<corner>.lib`` (Sky130,
    GF180MCU) or ``<library>_<corner>.lib`` (IHP SG13G2)."""
    found: dict[str, Path] = {}
    for path in sorted(lib_dir.glob(f"{library}_*.lib")):
        rest = path.stem[len(library) :]
        if rest.startswith("__"):
            found[rest[2:]] = path
        elif "__" not in rest:
            found.setdefault(rest[1:], path)
    return found


def _corner(lib_dir: Path, library: str, corner: str | None) -> Path:
    found = _corners(lib_dir, library)
    if corner:
        if corner not in found:
            raise PdkError(
                f"{library} has no corner {corner} (it has: {', '.join(sorted(found))})"
            )
        return found[corner]
    if not found:
        raise PdkError(f"{lib_dir} has no Liberty file for {library}")
    # The supply the library is named for (GF180MCU's mcu7t5v0, osu_sc_gp9t3v3).
    supply = re.search(r"(\d)v(\d)(?!.*\dv\d)", library)
    named = [f"tt_025C_{supply[1]}v{supply[2]}0"] if supply else []
    for nominal in (*named, *NOMINAL_CORNERS):
        if nominal in found:
            return found[nominal]
    # Else a typical corner at 25 C, else any typical, else the first.
    for prefer in ("tt_025C_", "tt_", "typ_"):
        typical = [found[c] for c in sorted(found) if c.startswith(prefer)]
        if typical:
            return typical[0]
    return found[sorted(found)[0]]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


_MODULE = re.compile(r"^\s*module\s+(\w+)\s*\((.*?)^\s*endmodule", re.M | re.S)
_BASE = re.compile(r"^\s*(\w+)\s+base\s*\(", re.M)
_DRIVE = r"_(?:\d+|lp\d*|m)"
_BARE_ENDIF = re.compile(r"^(\s*`endif)[ \t]+(?!//)(\w.*)$", re.M)


def _missing_base_models(text: str) -> str:
    """The base cells a library's sized cells wrap (``<cell>_1`` instantiating
    ``<cell> base (...)``) that its models never define -- open_pdks'
    sky130_fd_sc_lp lacks 111 -- each a copy of a sized variant's own model,
    renamed: drive strengths share the cell's function. Empty where every base is
    defined."""
    modules: dict[str, list[str]] = {}
    for name, body in _MODULE.findall(text):
        modules.setdefault(name, []).append(body)
    added = []
    for base in sorted(set(_BASE.findall(text)) - set(modules)):
        variants = sorted(
            (
                name
                for name, bodies in modules.items()
                if re.fullmatch(re.escape(base) + _DRIVE, name)
                and not any(_BASE.search(b) for b in bodies)
            ),
            key=lambda name: (not name.endswith("_1"), name),
        )
        if not variants:
            continue  # iverilog will name it: nothing here models it
        guard = variants[0].upper()
        start = text.find(f"`ifndef {guard}_V\n")
        close = f"`endif  // {guard}_V"
        end = text.find(close, start)
        if start < 0 or end < 0:
            continue
        section = text[start : end + len(close)]
        section = section.replace(f"{guard}_", f"{base.upper()}_BASE_")
        section = re.sub(rf"\b{re.escape(variants[0])}\b", base, section)
        added.append(f"// {base}: the model of {variants[0]}\n{section}\n")
    return "".join(added)


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
            f"(ciel fetch --pdk-family {ciel_family(pdk)} -l all <version>, then ciel "
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
    cells = verilog / f"{library}.v"
    if not cells.exists():
        raise PdkError(f"{verilog} has no {library}.v (the cells' Verilog models)")
    # The primitives the cells use, first: Sky130's and GF180MCU's primitives.v,
    # IHP's sg13g2_udp.v. The blackbox (empty) models are left out.
    sources = [
        *(
            v
            for v in sorted(verilog.glob("*.v"))
            if v != cells and "blackbox" not in v.name
        ),
        cells,
    ]
    text = "".join(
        f"// {src}\n" + src.read_text(encoding="utf-8", errors="replace") + "\n"
        for src in sources
        if src.exists()
    )
    # A bare word after `endif (sky130_fd_sc_hd__lpflow_bleeder in some open_pdks
    # versions) is a syntax error to iverilog: a comment.
    text = _BARE_ENDIF.sub(r"\1 // \2", text)
    text += _missing_base_models(text)
    if (
        not models.exists()
        or models.read_text(encoding="utf-8", errors="replace") != text
    ):
        models.write_text(text, encoding="utf-8")
    listed = json.loads(report.read_text(encoding="utf-8"))
    dont_use = tuple(sorted({str(r["cell"]) for r in listed}))
    return ResolvedLibrary(pdk, library, liberty, models, cell_map, dont_use)
