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
    name: str, cell_map: Path, liberty: Path, verilog_models: Path
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
    )


def get_profile(name: str, *, pdk_root: Path | None = None) -> TechnologyProfile:
    """A shipped profile (sky130, osu035), or ``<pdk>/<library>`` resolved from the
    PDK root (``pdk_root``, else $PDK_ROOT, else ~/.ciel; faultflow.pdk.resolve)."""
    key = name.strip()
    if key.lower() in _PROFILES:
        return _PROFILES[key.lower()]
    if "/" in key:
        from faultflow.pdk.resolve import resolve_library
        from faultflow.pdk.roles import PdkError

        pdk, library = key.split("/", 1)
        try:
            found = resolve_library(pdk, library, root=pdk_root)
        except PdkError as exc:
            raise ConfigError(str(exc)) from exc
        return _library_profile(
            key, found.cell_map, found.liberty, found.verilog_models
        )
    raise ConfigError(
        f"unknown technology profile: {name} (sky130, osu035, or <pdk>/<library> "
        "such as sky130A/sky130_fd_sc_hs)"
    )


def profile_for_config(cfg: "FaultflowConfig") -> TechnologyProfile:
    """The profile of the library ``cfg`` is on: a shipped one, or its own."""
    try:
        return profile_for_cell_map(cfg.cell_lib)
    except ConfigError:
        pass
    if cfg.std_cell_library:
        name = f"{cfg.pdk}/{cfg.std_cell_library}"
    else:
        name = cfg.cell_lib.stem
    if cfg.liberty is None or cfg.verilog_models is None:
        raise ConfigError(f"the library {name} needs a liberty and verilog_models")
    return _library_profile(name, cfg.cell_lib, cfg.liberty, cfg.verilog_models)


def profile_for_cell_map(path: Path) -> TechnologyProfile:
    requested = path.resolve()
    for profile in _PROFILES.values():
        if profile.cell_map.resolve() == requested:
            return profile
    raise ConfigError(f"cell map is not registered to a technology profile: {path}")
