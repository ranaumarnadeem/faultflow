from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultflow.config import (
    SKY130_CELL_LIB,
    SKY130_LIBERTY,
    SKY130_VERILOG_MODELS,
    ConfigError,
)


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


def get_profile(name: str) -> TechnologyProfile:
    try:
        return _PROFILES[name.strip().lower()]
    except KeyError as exc:
        raise ConfigError(f"unknown technology profile: {name}") from exc


def profile_for_cell_map(path: Path) -> TechnologyProfile:
    requested = path.resolve()
    for profile in _PROFILES.values():
        if profile.cell_map.resolve() == requested:
            return profile
    raise ConfigError(f"cell map is not registered to a technology profile: {path}")
