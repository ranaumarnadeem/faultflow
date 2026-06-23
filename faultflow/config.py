from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

FAULTFLOW_WORKSPACE = ".faultflow"

SKY130_CELL_LIB = Path("cells/sky130/sky130_fd_sc_hd.json")
SKY130_LIBERTY = Path("cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib")
SKY130_VERILOG_MODELS = Path("cells/sky130/sky130_fd_sc_hd.v")

BENCHMARK_SYNTH_ROOTS = (
    Path("tests/benchmarks/iscas85/synth"),
    Path("tests/benchmarks/iscas89/synth"),
)


def benchmark_synth_json(top: str) -> list[Path]:
    return [root / f"{top}.json" for root in BENCHMARK_SYNTH_ROOTS]


def benchmark_synth_bench(top: str) -> list[Path]:
    return [root / f"{top}.bench" for root in BENCHMARK_SYNTH_ROOTS]


def benchmark_synth_test(top: str) -> list[Path]:
    return [root / f"{top}atpg.test" for root in BENCHMARK_SYNTH_ROOTS]


def benchmark_synth_gate_verilog(top: str) -> list[Path]:
    names = (f"{top}_gate.v", f"{top}_synth.v", f"{top}.nl.v", f"{top}.cut.v")
    return [root / name for root in BENCHMARK_SYNTH_ROOTS for name in names]


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
    model: str = "stuck_at"
    launch: str = "loc"
    collapsing: bool = False
    include_clock_faults: bool = False
    include_reset_faults: bool = False


@dataclass(frozen=True)
class SimulationConfig:
    unsupported_cells: str = "fail"
    verify: bool = False
    verify_tool: str = "iverilog"
    # Tie Yosys "x"/"z" constant bits to 0 before simulation.
    # Required for netlists with unconnected/don't-care inputs (e.g. unused scan pins).
    tie_xz: bool = False


@dataclass(frozen=True)
class AtpgConfig:
    tool: str = "native"
    mode: str = "comb"
    output: Path = Path("patterns.test")
    random_vectors: int = 64
    sat_conflict_limit: int = 100000
    max_rounds: int = 20
    sat_timeout_seconds: int = 10
    compaction: str = "reverse"
    # When true, an accepted+verified SAT pattern is fault-simulated against ALL
    # remaining undetected faults this round (dropping fortuitous detections),
    # not just its target fault. Coverage-identical; cuts SAT calls + raw vectors.
    fault_drop_sat: bool = True
    # When true, restrict each combinational stuck-at per-fault CNF to the fault's
    # cone of influence (provably verdict-equivalent; cuts SAT time). Applies to
    # the FUNCTIONAL stuck-at path only for now.
    cone_restrict: bool = True
    # Dynamic compaction (compaction=dynamic): number of secondary-fault packing
    # orders to try, keeping the fewest-vector result (PO-DTC). 1 = single order.
    pack_orders: int = 1


@dataclass(frozen=True)
class ReportConfig:
    output: Path = Path("coverage.rpt")
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
class ClockSpec:
    """A declared clock domain (Phase 6, `add_clock` / `[clocks]`).

    ``off_state`` is the clock's inactive level (0 for active-high/posedge,
    1 for a negedge clock); the test protocol returns each clock to its
    off-state between pulses.
    """

    port: str
    off_state: int = 0


@dataclass(frozen=True)
class TestpointConfig:
    opentest: Path = Path("opentest")
    metric: str = "scoap"
    threshold: int = 50
    max_points: int = 10


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
    output_root: Path = Path("output")
    clocks: tuple[ClockSpec, ...] = ()
    blackbox_instances: tuple[str, ...] = ()
    testpoint: TestpointConfig = TestpointConfig()
    # IEEE 1500 wrapper test mode: "functional" | "intest" | "extest". INTEST and
    # EXTEST reconfigure the wrapper boundary control/observe points, so they are
    # distinct runs (part of the fingerprint).
    test_mode: str = "functional"
    # IEEE 1500 wrapper boundary cell model: "buffer" (transparent $wbc_*_faultflow)
    # or "scan" (native shiftable $wbc_*_scan_faultflow, Stage 4). The scan model
    # adds the wrapper boundary register as a real scan chain — control/observe
    # points and fault sites differ, so it is part of the fingerprint.
    wbr_model: str = "scan"

    @property
    def output_dir(self) -> Path:
        return self.output_root / self.top

    @property
    def workspace_dir(self) -> Path:
        return self.output_dir / FAULTFLOW_WORKSPACE

    @property
    def db_path(self) -> Path:
        return self.workspace_dir / "faultflow.sqlite"

    @property
    def logs_dir(self) -> Path:
        return self.workspace_dir / "logs"

    @property
    def manifests_dir(self) -> Path:
        return self.workspace_dir / "manifests"

    @property
    def intermediate_dir(self) -> Path:
        return self.workspace_dir / "intermediate"

    @property
    def verification_dir(self) -> Path:
        return self.workspace_dir / "verification"

    @property
    def generated_scripts_dir(self) -> Path:
        return self.workspace_dir / "generated_scripts"

    @property
    def scan_json_path(self) -> Path:
        return self.output_dir / f"{self.top}_scan.json"

    @property
    def scan_verilog_path(self) -> Path:
        return self.output_dir / f"{self.top}_scan.v"

    @property
    def scan_report_path(self) -> Path:
        return self.output_dir / "scan.rpt"

    @property
    def scan_manifest_path(self) -> Path:
        return self.manifests_dir / "scan_manifest.json"

    @property
    def coverage_report_path(self) -> Path:
        if self.report.output.is_absolute():
            return self.report.output
        if self.report.output.parent == Path("."):
            return self.output_dir / self.report.output.name
        return self.report.output

    @property
    def coverage_json_path(self) -> Path:
        return self.intermediate_dir / "coverage_report.json"

    @property
    def patterns_path(self) -> Path:
        path = self.atpg.output
        if path.is_absolute():
            return path
        if path.parent == Path("."):
            return self.output_dir / path.name
        return path

    def ensure_workspace(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.logs_dir,
            self.manifests_dir,
            self.intermediate_dir,
            self.verification_dir,
            self.generated_scripts_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


LEGACY_SCAN_DB_NAME = "faultflow_scan.sqlite"


def _path(parser: ConfigParser, section: str, key: str, default: str) -> Path:
    return Path(parser.get(section, key, fallback=default))


def _optional_path(parser: ConfigParser, section: str, key: str) -> Path | None:
    value = parser.get(section, key, fallback="").strip()
    return Path(value) if value else None


def _parse_clocks(parser: ConfigParser) -> tuple[ClockSpec, ...]:
    if not parser.has_section("clocks"):
        return ()

    ports_raw = parser.get("clocks", "ports", fallback="").strip()
    if not ports_raw:
        return ()

    ports = [p.strip() for p in ports_raw.split(",") if p.strip()]

    seen: set[str] = set()
    for p in ports:
        if p in seen:
            raise ConfigError(f"[clocks] duplicate port '{p}'")
        seen.add(p)

    off_states: dict[str, int] = {}
    off_raw = parser.get("clocks", "off", fallback="").strip()
    if off_raw:
        for token in off_raw.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" not in token:
                raise ConfigError(
                    f"[clocks] off entry '{token}' must be '<port>:<0|1>'"
                )
            port, _, val = token.partition(":")
            port = port.strip()
            val = val.strip()
            if port not in seen:
                raise ConfigError(f"[clocks] off references undeclared port '{port}'")
            if val not in {"0", "1"}:
                raise ConfigError(
                    f"[clocks] off_state for '{port}' must be 0 or 1, got '{val}'"
                )
            off_states[port] = int(val)

    return tuple(ClockSpec(port=p, off_state=off_states.get(p, 0)) for p in ports)


def _parse_blackbox(parser: ConfigParser) -> tuple[str, ...]:
    """Parse the optional ``[blackbox]`` section.

    ``instances = u_sram, u_pll`` — each named instance is modeled as a test
    boundary (inputs observable, outputs controllable). Empty/duplicate entries
    are rejected.
    """
    if not parser.has_section("blackbox"):
        return ()

    raw = parser.get("blackbox", "instances", fallback="").strip()
    if not raw:
        return ()

    instances = [name.strip() for name in raw.split(",") if name.strip()]
    seen: set[str] = set()
    for name in instances:
        if name in seen:
            raise ConfigError(f"[blackbox] duplicate instance '{name}'")
        seen.add(name)
    return tuple(instances)


def _verilog_models(parser: ConfigParser) -> Path:
    value = parser.get("simulation", "verilog_models", fallback="").strip()
    if not value:
        value = parser.get("design", "verilog_models", fallback="").strip()
    return Path(value) if value else SKY130_VERILOG_MODELS


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

    atpg_tool = parser.get("atpg", "tool", fallback="native")
    if atpg_tool not in {"native", "sat_atpg", "quaigh"}:
        raise ConfigError("atpg.tool must be native, sat_atpg, or quaigh")

    atpg_mode = parser.get("atpg", "mode", fallback="comb")
    if atpg_mode != "comb":
        raise ConfigError("Only atpg.mode=comb is supported")

    atpg_compaction = parser.get("atpg", "compaction", fallback="reverse")
    if atpg_compaction not in {"none", "reverse", "dynamic"}:
        raise ConfigError("atpg.compaction must be 'none', 'reverse', or 'dynamic'")

    # Fault model: `model` is canonical; `type` is a back-compat alias (older
    # configs carried `type = stuck_at`). Prefer `model` when both are present.
    fault_model = parser.get(
        "fault_model",
        "model",
        fallback=parser.get("fault_model", "type", fallback="stuck_at"),
    ).strip()
    if fault_model not in {"stuck_at", "transition"}:
        raise ConfigError("fault_model.model must be 'stuck_at' or 'transition'")

    fault_launch = parser.get("fault_model", "launch", fallback="loc").strip()
    if fault_launch not in {"loc", "los"}:
        raise ConfigError("fault_model.launch must be 'loc' or 'los'")

    fault_collapsing = _bool(parser, "fault_model", "collapsing", False)
    if fault_model == "transition" and fault_collapsing:
        raise ConfigError(
            "fault collapsing is not supported for the transition model "
            "(stuck-at equivalence rules do not hold for transition faults)"
        )

    clocks = _parse_clocks(parser)
    blackbox_instances = _parse_blackbox(parser)

    test_mode = parser.get("testmode", "mode", fallback="functional").strip().lower()
    if test_mode not in {"functional", "intest", "extest"}:
        raise ConfigError("testmode.mode must be 'functional', 'intest', or 'extest'")

    wbr_model = parser.get("wrap", "wbr_model", fallback="scan").strip().lower()
    if wbr_model not in {"buffer", "scan"}:
        raise ConfigError("wrap.wbr_model must be 'buffer' or 'scan'")

    return FaultflowConfig(
        path=cfg_path,
        top=top,
        netlist=_path(parser, "design", "netlist", "design.json"),
        cell_lib=_path(parser, "design", "cell_lib", str(SKY130_CELL_LIB)),
        liberty=_optional_path(parser, "design", "liberty") or SKY130_LIBERTY,
        verilog_models=_verilog_models(parser),
        yosys_ver=parser.get("design", "yosys_ver", fallback=""),
        fault_model=FaultModelConfig(
            model=fault_model,
            launch=fault_launch,
            collapsing=fault_collapsing,
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
            tie_xz=_bool(parser, "simulation", "tie_xz", False),
        ),
        atpg=AtpgConfig(
            tool=atpg_tool,
            mode=atpg_mode,
            output=_path(parser, "atpg", "output", "patterns.test"),
            random_vectors=_int(parser, "atpg", "random_vectors", 64),
            sat_conflict_limit=_int(parser, "atpg", "sat_conflict_limit", 100000),
            max_rounds=_int(parser, "atpg", "max_rounds", 20),
            sat_timeout_seconds=_int(parser, "atpg", "sat_timeout_seconds", 10),
            compaction=atpg_compaction,
            fault_drop_sat=_bool(parser, "atpg", "fault_drop_sat", True),
            cone_restrict=_bool(parser, "atpg", "cone_restrict", True),
            pack_orders=_int(parser, "atpg", "pack_orders", 1),
        ),
        report=ReportConfig(
            output=_path(parser, "report", "output", "coverage.rpt"),
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
        clocks=clocks,
        blackbox_instances=blackbox_instances,
        testpoint=TestpointConfig(
            opentest=Path(parser.get("testpoint", "opentest", fallback="opentest")),
            metric=parser.get("testpoint", "metric", fallback="scoap"),
            threshold=_int(parser, "testpoint", "threshold", 50),
            max_points=_int(parser, "testpoint", "max_points", 10),
        ),
        test_mode=test_mode,
        wbr_model=wbr_model,
    )
