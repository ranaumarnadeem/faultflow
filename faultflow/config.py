from __future__ import annotations

import os
import re
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


def parse_timeout_schedule(value: str, fallback: int) -> list[int]:
    """Parse the escalating SAT-timeout schedule string into a list of seconds.

    The schedule is a comma-separated list of positive integers, smallest first
    (e.g. ``"2,10,60"``): a fault is first attempted with the smallest timeout
    and only escalates to a longer one if it times out. An empty string means
    "no schedule" and yields a single tier equal to ``fallback`` (the plain
    ``sat_timeout_seconds``), which reproduces the original fixed-timeout
    behaviour exactly.
    """
    text = value.strip()
    if not text:
        return [fallback]
    tiers: list[int] = []
    for token in text.split(","):
        token = token.strip()
        try:
            seconds = int(token)
        except ValueError:
            raise ConfigError(
                f"sat_timeout_schedule entries must be integers, got {token!r}"
            )
        if seconds < 1:
            raise ConfigError(
                f"sat_timeout_schedule entries must be >= 1 second, got {seconds}"
            )
        tiers.append(seconds)
    return tiers


def interleave_easy_hard(sorted_items: list, workers: int, easy_reserve: int) -> list:
    """Reorder a sorted fault list for mixed easy/hard parallel submission.

    ``sorted_items`` must be sorted easy→hard (e.g. by ``(is_reconv, cone_size)``
    ascending).  When ``workers >= 4`` and ``easy_reserve > 0``, each chunk of
    ``workers`` items in the returned list contains ``easy_reserve`` items from
    the front (easy/small-cone) and ``workers - easy_reserve`` items from the
    back (hard/reconvergent).  The executor then always has a mix of fast and
    slow faults running concurrently — easy slots free up quickly and are
    refilled from the remaining easy pool, while hard slots stay busy for
    their full SAT budget.

    Returns the input list unchanged when workers < 4, easy_reserve <= 0, or
    easy_reserve >= workers (degenerate cases that collapse to a single queue).
    """
    n = len(sorted_items)
    if n == 0 or workers < 4 or easy_reserve <= 0 or easy_reserve >= workers:
        return list(sorted_items)

    result: list = []
    front = 0
    back = n - 1
    hard_per_chunk = workers - easy_reserve

    while front <= back:
        easy_chunk: list = []
        while len(easy_chunk) < easy_reserve and front <= back:
            easy_chunk.append(sorted_items[front])
            front += 1
        # Pull hard items from the back; hardest (highest index) submitted first
        # so they start as early as possible within the worker pool.
        hard_chunk: list = []
        while len(hard_chunk) < hard_per_chunk and back >= front:
            hard_chunk.append(sorted_items[back])
            back -= 1
        result.extend(easy_chunk)
        result.extend(hard_chunk)

    return result


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
    collapsing: bool = True
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
    # Threads used to parallelize fault GRADING (the dominant ATPG cost). 1 keeps
    # the original serial behaviour; 0 = auto (cpu_count - 2, min 1); N uses N
    # threads. Each thread owns its SimState over the shared immutable graph; the
    # detected-set merge and the single SQLite writer stay on the calling thread,
    # so coverage is bit-identical for any value. See resolve_sim_threads().
    sim_threads: int = 1


def resolve_sim_threads(sim_threads: int) -> int:
    """Resolve a configured sim_threads value to a concrete worker count.

    1 (or any positive N) is taken literally; 0 means auto-detect, leaving two
    logical CPUs as headroom for the main loop / SAT waves. Always >= 1.
    """
    if sim_threads > 0:
        return sim_threads
    return max(1, (os.cpu_count() or 1) - 2)


@dataclass(frozen=True)
class AtpgConfig:
    tool: str = "native"
    mode: str = "comb"
    output: Path = Path("patterns.test")
    random_vectors: int = 64
    sat_conflict_limit: int = 100000
    max_rounds: int = 20
    sat_timeout_seconds: int = 10
    # Escalating per-fault SAT timeout. Empty string keeps the single fixed
    # sat_timeout_seconds (original behaviour). A comma-separated list like
    # "2,10,60" attempts each fault at the smallest timeout first and only
    # escalates a fault that times out, so easy faults clear fast and long
    # budgets are reserved for hard ones. Parsed by parse_timeout_schedule().
    sat_timeout_schedule: str = "2,10,60"
    compaction: str = "reverse"
    # When true, an accepted+verified SAT pattern is fault-simulated against ALL
    # remaining undetected faults this round (dropping fortuitous detections),
    # not just its target fault. Coverage-identical; cuts SAT calls + raw vectors.
    fault_drop_sat: bool = True
    # When true, restrict each combinational stuck-at per-fault CNF to the fault's
    # cone of influence (provably verdict-equivalent; cuts SAT time). Applies to
    # the FUNCTIONAL stuck-at path only for now.
    cone_restrict: bool = True
    # When true, use the incremental fan-in cones (IFC) stuck-at solver: encode one
    # reached observable's cone at a time instead of the whole cone up front
    # (verdict-equivalent; cuts CNF-generation time on large designs). FUNCTIONAL
    # stuck-at path only. Verdict-identical like cone_restrict, so it has no resume
    # fingerprint column.
    incremental_sat: bool = False
    # Dynamic compaction (compaction=dynamic): number of secondary-fault packing
    # orders to try, keeping the fewest-vector result (PO-DTC). 1 = single order.
    pack_orders: int = 1
    # Order faults by ascending cone size (smallest structural cone first) so the
    # quickest SAT calls happen first and detect more faults incidentally early.
    # False keeps the original database (enumeration) order. Coverage-identical.
    order_by_cone_size: bool = True
    # Parallel fault-simulation workers (0 = auto-detect logical CPU count).
    # Each worker owns its own SimState over the shared immutable CompiledSimGraph.
    # The coordinator merges detected sets; the DB writer remains single-threaded.
    workers: int = 1
    # Run OT structural reconvergence analysis before ATPG (requires opentest on PATH).
    # Phase A: reconvergent-site faults are sorted last and skip the short timeout tier.
    # Phase B: canceling-path stems are marked UNSAT without any SAT call.
    # Falls back silently when opentest is unavailable.
    preflight: bool = True
    # PDK tech tag passed to opentest _preflight. Empty string = auto-detect from
    # the cell_lib path (sky130 unless the path contains "osu035" or "osu").
    preflight_tech: str = ""
    # When workers >= 4 and this is > 0, each parallel submission wave reserves
    # this many slots for easy faults (small-cone / non-reconvergent, from the
    # front of the sorted list) while the remaining workers - easy_fault_reserve
    # slots run hard faults (large-cone / reconvergent, from the back). This
    # keeps fast and slow faults running concurrently so easy slots cycle
    # quickly while hard slots use their full timeout budget. Has no effect
    # when workers < 4 or easy_fault_reserve <= 0 (degenerate: all one queue).
    easy_fault_reserve: int = 2


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


def add_clock_to_config(path: str | Path, port: str, *, off_state: int = 0) -> None:
    """Declare a clock in a config.ofs file's ``[clocks]`` section, in place.

    Mirrors the Tcl shell's ``add_clock`` (dedup by port; redeclaring a port
    replaces its off_state), but persists directly to the config file since a
    one-shot CLI invocation has no interactive session to hold the declaration
    in. Edits only the ``ports``/``off`` lines of ``[clocks]`` (adding the
    section if absent) so the rest of the file, including comments, is left
    untouched -- unlike a full ``ConfigParser.write()`` round-trip.
    """
    if off_state not in (0, 1):
        raise ConfigError(f"add_clock: off_state must be 0 or 1, got {off_state}")
    port = port.strip()
    if not port:
        raise ConfigError("add_clock: port must not be empty")

    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")

    parser = ConfigParser()
    if not parser.read(cfg_path):
        raise ConfigError(f"config file not found: {cfg_path}")
    existing = _parse_clocks(parser)
    updated = tuple(cs for cs in existing if cs.port != port) + (
        ClockSpec(port=port, off_state=off_state),
    )

    ports_line = "ports = " + ", ".join(cs.port for cs in updated)
    off_tokens = [f"{cs.port}:{cs.off_state}" for cs in updated if cs.off_state != 0]
    off_line = "off = " + ", ".join(off_tokens) if off_tokens else None

    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    section_start = next(
        (i for i, ln in enumerate(lines) if re.match(r"^\s*\[clocks\]\s*$", ln)), None
    )

    if section_start is None:
        block = ["", "[clocks]", ports_line]
        if off_line:
            block.append(off_line)
        new_text = text.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    else:
        section_end = len(lines)
        for i in range(section_start + 1, len(lines)):
            if re.match(r"^\s*\[[^\]]+\]\s*$", lines[i]):
                section_end = i
                break
        body = lines[section_start + 1 : section_end]
        kept = [
            ln
            for ln in body
            if not re.match(r"^\s*ports\s*=", ln) and not re.match(r"^\s*off\s*=", ln)
        ]
        new_body = [ports_line] + ([off_line] if off_line else []) + kept
        lines[section_start + 1 : section_end] = new_body
        new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    cfg_path.write_text(new_text, encoding="utf-8")


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

    try:
        sim_threads = parser.getint("simulation", "sim_threads", fallback=1)
    except ValueError:
        raise ConfigError("sim_threads must be an integer")
    if sim_threads < 0:
        raise ConfigError("sim_threads must be >= 0 (0 = auto)")

    atpg_tool = parser.get("atpg", "tool", fallback="native")
    if atpg_tool not in {"native", "sat_atpg", "quaigh"}:
        raise ConfigError("atpg.tool must be native, sat_atpg, or quaigh")

    atpg_mode = parser.get("atpg", "mode", fallback="comb")
    if atpg_mode != "comb":
        raise ConfigError("Only atpg.mode=comb is supported")

    atpg_compaction = parser.get("atpg", "compaction", fallback="reverse")
    if atpg_compaction not in {"none", "reverse", "dynamic"}:
        raise ConfigError("atpg.compaction must be 'none', 'reverse', or 'dynamic'")

    atpg_sat_timeout_schedule = parser.get(
        "atpg", "sat_timeout_schedule", fallback="2,10,60"
    ).strip()
    # Validate eagerly so a malformed schedule fails at config load, not mid-run.
    parse_timeout_schedule(
        atpg_sat_timeout_schedule, _int(parser, "atpg", "sat_timeout_seconds", 10)
    )

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

    fault_collapsing = _bool(parser, "fault_model", "collapsing", True)
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
            sim_threads=sim_threads,
        ),
        atpg=AtpgConfig(
            tool=atpg_tool,
            mode=atpg_mode,
            output=_path(parser, "atpg", "output", "patterns.test"),
            random_vectors=_int(parser, "atpg", "random_vectors", 64),
            sat_conflict_limit=_int(parser, "atpg", "sat_conflict_limit", 100000),
            max_rounds=_int(parser, "atpg", "max_rounds", 20),
            sat_timeout_seconds=_int(parser, "atpg", "sat_timeout_seconds", 10),
            sat_timeout_schedule=atpg_sat_timeout_schedule,
            compaction=atpg_compaction,
            fault_drop_sat=_bool(parser, "atpg", "fault_drop_sat", True),
            cone_restrict=_bool(parser, "atpg", "cone_restrict", True),
            incremental_sat=_bool(parser, "atpg", "incremental_sat", False),
            pack_orders=_int(parser, "atpg", "pack_orders", 1),
            order_by_cone_size=_bool(parser, "atpg", "order_by_cone_size", True),
            workers=_int(parser, "atpg", "workers", 1),
            preflight=_bool(parser, "atpg", "preflight", True),
            preflight_tech=parser.get("atpg", "preflight_tech", fallback="").strip(),
            easy_fault_reserve=_int(parser, "atpg", "easy_fault_reserve", 2),
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
