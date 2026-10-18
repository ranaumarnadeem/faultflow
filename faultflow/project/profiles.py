from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from faultflow.config import (
    SKY130_CELL_LIB,
    SKY130_LIBERTY,
    SKY130_VERILOG_MODELS,
    ConfigError,
)

if TYPE_CHECKING:
    from faultflow.config import FaultflowConfig


@dataclass(frozen=True)
class TechnologyProfile:
    name: str
    cell_map: Path
    liberty: Path
    verilog_models: Path
    supports_generic_scan: bool
    supports_physical_scan: bool
    # Every Liberty file synthesis reads as cells (FaultflowConfig.liberty_files):
    # empty for `liberty` alone.
    liberty_files: tuple[Path, ...] = ()
    # [design] extra_libraries: a <pdk>/<library>+<extra>... profile's extras.
    extra_libraries: tuple[str, ...] = ()


_PROFILES = {
    "sky130": TechnologyProfile(
        name="sky130",
        cell_map=SKY130_CELL_LIB,
        liberty=SKY130_LIBERTY,
        verilog_models=SKY130_VERILOG_MODELS,
        supports_generic_scan=True,
        supports_physical_scan=True,
    ),
    "osu035": TechnologyProfile(
        name="osu035",
        cell_map=Path("cells/osu/osu035.json"),
        liberty=Path("cells/osu/osu035_stdcells.lib"),
        verilog_models=Path("cells/osu/osu035_stdcells.v"),
        supports_generic_scan=True,
        supports_physical_scan=False,
    ),
}


def _library_profile(
    name: str,
    cell_map: Path,
    liberty: Path,
    verilog_models: Path,
    liberty_files: tuple[Path, ...] = (),
    extra_libraries: tuple[str, ...] = (),
) -> TechnologyProfile:
    """A profile of any library: physical scan where it has a scan flop
    (faultflow.pdk.roles)."""
    from faultflow.pdk.roles import PdkError, library_roles

    loaded = json.loads(Path(cell_map).read_text(encoding="utf-8"))
    try:
        library_roles(loaded)["scan_ff"]
        physical = True
    except PdkError:
        physical = False
    return TechnologyProfile(
        name=name,
        cell_map=Path(cell_map),
        liberty=Path(liberty),
        verilog_models=Path(verilog_models),
        supports_generic_scan=True,
        supports_physical_scan=physical,
        liberty_files=liberty_files,
        extra_libraries=extra_libraries,
    )


def get_profile(name: str, *, pdk_root: Path | None = None) -> TechnologyProfile:
    """A shipped profile (sky130, osu035), or ``<pdk>/<library>`` resolved from the
    PDK root (``pdk_root``, else $PDK_ROOT, else ~/.ciel; faultflow.pdk.resolve),
    with ``+<library>`` for each extra library of the PDK the design instantiates
    cells of ([design] extra_libraries), as in
    ``sky130A/sky130_fd_sc_hd+sky130_fd_sc_hvl``."""
    key = name.strip()
    if key.lower() in _PROFILES:
        return _PROFILES[key.lower()]
    if "/" in key:
        from faultflow.pdk.resolve import combine_libraries, resolve_library
        from faultflow.pdk.roles import PdkError

        pdk, libraries = key.split("/", 1)
        library, *extra_names = [n.strip() for n in libraries.split("+")]
        try:
            found = resolve_library(pdk, library, root=pdk_root)
            extras = [resolve_library(pdk, n, root=pdk_root) for n in extra_names]
            cell_map, models = (
                combine_libraries(found, extras)
                if extras
                else (found.cell_map, found.verilog_models)
            )
        except PdkError as exc:
            raise ConfigError(str(exc)) from exc
        return _library_profile(
            key,
            cell_map,
            found.liberty,
            models,
            liberty_files=(
                *found.liberty_files,
                *(path for extra in extras for path in extra.liberty_files),
            ),
            extra_libraries=tuple(extra.library for extra in extras),
        )
    raise ConfigError(
        f"unknown technology profile: {name} (sky130, osu035, or <pdk>/<library> "
        "such as sky130A/sky130_fd_sc_hs, +<library> for each extra library)"
    )


def profile_for_config(cfg: "FaultflowConfig") -> TechnologyProfile:
    """The profile of the library ``cfg`` is on: a shipped one, or its own."""
    try:
        return profile_for_cell_map(cfg.cell_lib)
    except ConfigError:
        pass
    if cfg.std_cell_library:
        name = "+".join([f"{cfg.pdk}/{cfg.std_cell_library}", *cfg.extra_libraries])
    else:
        name = cfg.cell_lib.stem
    if cfg.liberty is None or cfg.verilog_models is None:
        raise ConfigError(f"the library {name} needs a liberty and verilog_models")
    return _library_profile(
        name,
        cfg.cell_lib,
        cfg.liberty,
        cfg.verilog_models,
        liberty_files=cfg.liberty_files,
        extra_libraries=cfg.extra_libraries,
    )


def profile_for_cell_map(path: Path) -> TechnologyProfile:
    requested = path.resolve()
    for profile in _PROFILES.values():
        if profile.cell_map.resolve() == requested:
            return profile
    raise ConfigError(f"cell map is not registered to a technology profile: {path}")
