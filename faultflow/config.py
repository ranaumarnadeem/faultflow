from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def parse_bool_value(value: str, key: str = "boolean") -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise ConfigError(f"{key} must be one of true/false, 1/0, yes/no, or on/off")


def _bool(parser: ConfigParser, section: str, key: str, default: bool) -> bool:
    if not parser.has_option(section, key):
        return default
    return parse_bool_value(parser.get(section, key), key)


def _float(parser: ConfigParser, section: str, key: str, default: float) -> float:
    if not parser.has_option(section, key):
        return default
    return parser.getfloat(section, key)


def _int(parser: ConfigParser, section: str, key: str, default: int) -> int:
    if not parser.has_option(section, key):
        return default
    return parser.getint(section, key)


def _optional_int(parser: ConfigParser, section: str, key: str) -> int | None:
    value = parser.get(section, key, fallback="").strip()
    return int(value) if value else None


@dataclass(frozen=True)
class FaultModelConfig:
    collapsing: bool = False
    include_clock_faults: bool = False
    include_reset_faults: bool = False


@dataclass(frozen=True)
class SimulationConfig:
    unsupported_cells: str = "fail"
    verify: bool = False
    verify_tool: str = "iverilog"


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
class ScanConfig:
    chains: int = 1
    max_chain_length: int | None = None
    scan_in: str = "scan_in"
    scan_out: str = "scan_out"
    scan_enable: str = "scan_en"
    run_techmap: bool = True


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
    scan: ScanConfig

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


def _verilog_models(parser: ConfigParser) -> Path:
    value = parser.get("simulation", "verilog_models", fallback="").strip()
    if not value:
        value = parser.get("design", "verilog_models", fallback="").strip()
    return Path(value) if value else Path("cells/osu/osu035_stdcells.v")


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

    verify_tool = parser.get("simulation", "verify_tool", fallback="iverilog")
    if verify_tool != "iverilog":
        raise ConfigError("verify_tool must be 'iverilog'")

    atpg_mode = parser.get("atpg", "mode", fallback="comb")
    if atpg_mode != "comb":
        raise ConfigError("Only atpg.mode=comb is supported")

    return FaultflowConfig(
        path=cfg_path,
        top=top,
        netlist=_path(parser, "design", "netlist", "design.json"),
        cell_lib=_path(parser, "design", "cell_lib", "cells/osu/osu035.json"),
        liberty=_optional_path(parser, "design", "liberty"),
        verilog_models=_verilog_models(parser),
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
        simulation=SimulationConfig(
            unsupported_cells=unsupported,
            verify=_bool(parser, "simulation", "verify", False),
            verify_tool=verify_tool,
        ),
        atpg=AtpgConfig(
            tool=parser.get("atpg", "tool", fallback="quaigh"),
            mode=atpg_mode,
            output=_path(parser, "atpg", "output", f"{top}atpg.test"),
        ),
        report=ReportConfig(
            output=_path(parser, "report", "output", "coverage_report.json"),
            threshold=_float(parser, "report", "threshold", 95.0),
        ),
        scan=ScanConfig(
            chains=_int(parser, "scan", "chains", 1),
            max_chain_length=_optional_int(parser, "scan", "max_chain_length"),
            scan_in=parser.get("scan", "scan_in", fallback="scan_in"),
            scan_out=parser.get("scan", "scan_out", fallback="scan_out"),
            scan_enable=parser.get("scan", "scan_enable", fallback="scan_en"),
            run_techmap=_bool(parser, "scan", "run_techmap", True),
        ),
    )
