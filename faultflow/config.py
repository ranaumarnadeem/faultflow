from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _bool(parser: ConfigParser, section: str, key: str, default: bool) -> bool:
    if not parser.has_option(section, key):
        return default
    return parser.getboolean(section, key)


def _float(parser: ConfigParser, section: str, key: str, default: float) -> float:
    if not parser.has_option(section, key):
        return default
    return parser.getfloat(section, key)


@dataclass(frozen=True)
class FaultModelConfig:
    collapsing: bool = False
    include_clock_faults: bool = False
    include_reset_faults: bool = False


@dataclass(frozen=True)
class SimulationConfig:
    unsupported_cells: str = "fail"


@dataclass(frozen=True)
class AtpgConfig:
    tool: str = "quaigh"
    mode: str = "comb"
    output: Path = Path("atpg.test")


@dataclass(frozen=True)
class ReportConfig:
    output: Path = Path("coverage_report.json")
    threshold: float = 95.0


@dataclass(frozen=True)
class FaultflowConfig:
    path: Path
    top: str
    netlist: Path
    cell_lib: Path
    liberty: Path | None
    verilog_models: Path | None
    yosys_ver: str
    fault_model: FaultModelConfig
    simulation: SimulationConfig
    atpg: AtpgConfig
    report: ReportConfig

    @property
    def output_dir(self) -> Path:
        return Path("output") / self.top

    @property
    def db_path(self) -> Path:
        return self.output_dir / "faultflow.sqlite"


def _path(parser: ConfigParser, section: str, key: str, default: str) -> Path:
    return Path(parser.get(section, key, fallback=default))


def _optional_path(parser: ConfigParser, section: str, key: str) -> Path | None:
    value = parser.get(section, key, fallback="").strip()
    return Path(value) if value else None


def load_config(path: str | Path, top: str) -> FaultflowConfig:
    cfg_path = Path(path)
    parser = ConfigParser()
    read = parser.read(cfg_path)
    if not read:
        raise ConfigError(f"Cannot read config: {cfg_path}")

    if not top:
        raise ConfigError("--top is required")

    unsupported = parser.get("simulation", "unsupported_cells", fallback="fail")
    if unsupported not in {"fail", "blackbox"}:
        raise ConfigError("unsupported_cells must be 'fail' or 'blackbox'")

    atpg_mode = parser.get("atpg", "mode", fallback="comb")
    if atpg_mode != "comb":
        raise ConfigError("Phase 1 supports only atpg.mode=comb")

    return FaultflowConfig(
        path=cfg_path,
        top=top,
        netlist=_path(parser, "design", "netlist", "design.json"),
        cell_lib=_path(parser, "design", "cell_lib", "cells/osu/osu035.json"),
        liberty=_optional_path(parser, "design", "liberty"),
        verilog_models=_optional_path(parser, "design", "verilog_models"),
        yosys_ver=parser.get("design", "yosys_ver", fallback=""),
        fault_model=FaultModelConfig(
            collapsing=_bool(parser, "fault_model", "collapsing", False),
            include_clock_faults=_bool(
                parser, "fault_model", "include_clock_faults", False
            ),
            include_reset_faults=_bool(
                parser, "fault_model", "include_reset_faults", False
            ),
        ),
        simulation=SimulationConfig(unsupported_cells=unsupported),
        atpg=AtpgConfig(
            tool=parser.get("atpg", "tool", fallback="quaigh"),
            mode=atpg_mode,
            output=_path(parser, "atpg", "output", f"{top}atpg.test"),
        ),
        report=ReportConfig(
            output=_path(parser, "report", "output", "coverage_report.json"),
            threshold=_float(parser, "report", "threshold", 95.0),
        ),
    )
