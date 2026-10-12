"""``ff.py wsp``: grade the faults a block's scan test leaves to its IEEE 1500 WSP
(``excluded_wsp``, faultflow.wrap.wsp.wsp_sites) with the WSP's integrity program
(faultflow.wrap.wsp_program), compared at WSO, the wrapped outputs and the
wrapper chains' scan-outs.

The grade runs on the block's scanned netlist (the scan manifest's
``generic_json``), each fault found there by its site key. A program cycle is one
simulator step -- its inputs with WRCK low, WSO strobed -- or, when it pulses
WRCK, three: WRCK low, high and low again with the same inputs, the falling edge
the WIR's update stage takes. Every input but the WSP's and the wrapped data inputs
is 0.

The golden gate comes first: the netlist's fault-free outputs are the model's at
every care bit, from all-0 and all-1 flop states alike. Outputs that care at the
same cycles are simulated together, sampled at those cycles alone. A fault is
credited when a compared output differs from the fault-free one at a care bit from
both initial states; one that does from one of them only is left uncredited
(``init_dependent``). The credit
lands in side tables beside the scan campaign (``wsp_runs``, ``wsp_detections``),
and the coverage report gains a ``wsp`` block (:func:`report_block`) -- with the
scan's credit, the combined coverage.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from faultflow.config import FaultflowConfig
from faultflow.control_trace import port_bits
from faultflow.wrap.wsp_program import (
    WspProgram,
    WspStep,
    conformance_failures,
    program_tests,
)
from faultflow.wrap.wsp_setup import HELD


class WspGradeError(RuntimeError):
    """``ff.py wsp`` can't run as asked, or the netlist failed the golden gate; the
    message says why."""


@dataclass(frozen=True)
class WspGrade:
    graded: int
    detected: dict[int, tuple[str, int]]  # fault id -> (test, program cycle)
    init_dependent: tuple[int, ...]  # detected from one initial flop state only


@dataclass(frozen=True)
class WspOutcome:
    source: str
    graded: int
    detected: int
    init_dependent: int
    cycles: int
    seconds: float
    up_to_date: bool
    combined_coverage: float | None

    def message(self, top: str) -> str:
        combined = (
            "n/a"
            if self.combined_coverage is None
            else f"{self.combined_coverage:.3f}%"
        )
        return (
            f"wsp top={top} program={self.source} cycles={self.cycles} "
            f"graded={self.graded} "
            f"detected={self.detected} init_dependent={self.init_dependent} "
            f"combined_coverage={combined}"
            + (
                " up_to_date=true"
                if self.up_to_date
                else f" seconds={self.seconds:.2f}"
            )
        )


def program_digest(steps: Sequence[WspStep]) -> str:
    text = json.dumps(
        [
            [dataclasses.asdict(s.cycle), s.ports, s.test, s.wso, s.expected]
            for s in steps
        ],
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compared(steps: Sequence[WspStep], wso: str) -> dict[str, list[int | None]]:
    """Each output the program compares, WSO (port `wso`) first, with the value
    it expects at each cycle's strobe (None: no care)."""
    found: dict[str, list[int | None]] = {wso: [step.wso for step in steps]}
    for number, step in enumerate(steps):
        for name, value in step.expected:
            found.setdefault(name, [None] * len(steps))[number] = value
    return found


def sim_rows(
    steps: Sequence[WspStep],
    inputs: Sequence[str],
    wsp_ports: Mapping[str, str],
    care: Sequence[bool],
) -> tuple[list[list[bool]], list[bool], list[int]]:
    """The simulator's input rows (in `inputs` order), its sample flags -- the
    strobe of each cycle `care` marks -- and the program cycle of each sample.
    `wsp_ports`: each WSP input's port, by role."""
    index = {name: k for k, name in enumerate(inputs)}
    wrck = wsp_ports["wrck"]
    rows: list[list[bool]] = []
    sample: list[bool] = []
    at: list[int] = []
    for number, step in enumerate(steps):
        values = {wsp_ports[role]: int(getattr(step.cycle, role)) for role in HELD}
        values.update(dict(step.ports))
        unknown = sorted((set(values) | {wrck}) - set(index))
        if unknown:
            raise WspGradeError(f"the program drives {unknown[0]}, no input of it")
        for k, clock in enumerate((0, 1, 0) if step.cycle.pulse else (0,)):
            row = [False] * len(inputs)
            for name, value in values.items():
                row[index[name]] = bool(value)
            row[index[wrck]] = bool(clock)
            rows.append(row)
            strobe = k == 0 and care[number]
            sample.append(strobe)
            if strobe:
                at.append(number)
    return rows, sample, at


@dataclass(frozen=True)
class _Bench:
    """One simulation setup of the program: the outputs it compares, which care
    at the same cycles, and the samples at those cycles."""

    core: Any
    netlist: str
    cell_map: str
    inputs: list[str]
    rows: list[list[bool]]
    sample: list[bool]
    at: list[int]
    observe: list[str]
    unsupported: str
    blackbox: list[str]

    def run(
        self, faults: list[tuple[int, int]], initial: bool, threads: int = 1
    ) -> dict[str, Any]:
        result: dict[str, Any] = self.core.simulate_sequence_faults(
            self.netlist,
            self.cell_map,
            self.inputs,
            self.rows,
            self.sample,
            self.observe,
            faults,
            self.unsupported,
            self.blackbox,
            threads,
            False,
            initial,
        )
        return result


def benches(
    core: Any,
    netlist: str,
    cell_map: str,
    steps: Sequence[WspStep],
    inputs: Sequence[str],
    wsp_ports: Mapping[str, str],
    expected: Mapping[str, Sequence[int | None]],
    *,
    unsupported: str,
    blackbox: Sequence[str],
) -> list[_Bench]:
    """A bench for every set of compared outputs that care at the same cycles:
    each one's samples are its care bits alone."""
    groups: dict[tuple[bool, ...], list[str]] = {}
    for name, values in expected.items():
        care = tuple(value is not None for value in values)
        if any(care):
            groups.setdefault(care, []).append(name)
    found: list[_Bench] = []
    for care, names in groups.items():
        rows, sample, at = sim_rows(steps, inputs, wsp_ports, care)
        found.append(
            _Bench(
                core,
                netlist,
                cell_map,
                list(inputs),
                rows,
                sample,
                at,
                names,
                unsupported,
                list(blackbox),
            )
        )
    return found


def golden_gate(
    benches: Sequence[_Bench],
    steps: Sequence[WspStep],
    expected: Mapping[str, Sequence[int | None]],
) -> None:
    """Refuse a netlist whose fault-free outputs aren't the model's at a care bit,
    from either initial flop state."""
    for bench in benches:
        for initial in (False, True):
            golden = bench.run([], initial)["golden"]
            for sample, number in enumerate(bench.at):
                for column, name in enumerate(bench.observe):
                    want, got = expected[name][number], int(golden[sample][column])
                    if got != want:
                        raise WspGradeError(
                            f"the netlist's {name} disagrees with the WSP's model in "
                            f"test {steps[number].test} at program cycle {number}: "
                            f"the model gives {want}, the netlist {got} (flops "
                            f"starting at {int(initial)})"
                        )


def grade_faults(
    benches: Sequence[_Bench],
    steps: Sequence[WspStep],
    faults: Sequence[tuple[int, int, int]],
    threads: int = 1,
) -> WspGrade:
    """Grade `faults` -- (fault id, compiled net index, stuck value) -- after the
    golden gate: each one's first program cycle at which a compared output
    differs at a care bit, from both initial flop states."""
    specs = [(index, stuck) for _, index, stuck in faults]
    first: list[list[int | None]] = []
    for initial in (False, True):
        cycles: list[int | None] = [None] * len(faults)
        for bench in benches:
            found = bench.run(specs, initial, threads)["first_sample"]
            for k, sample in enumerate(found):
                if sample >= 0:
                    number = bench.at[sample]
                    if cycles[k] is None or number < int(cycles[k] or 0):
                        cycles[k] = number
        first.append(cycles)
    detected: dict[int, tuple[str, int]] = {}
    dependent: list[int] = []
    for (fault_id, _, _), zero, one in zip(faults, *first):
        if zero is not None and one is not None:
            number = max(zero, one)
            detected[fault_id] = (steps[number].test, number)
        elif zero is not None or one is not None:
            dependent.append(fault_id)
    return WspGrade(len(faults), detected, tuple(dependent))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS wsp_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    run_key TEXT NOT NULL,
    program_digest TEXT NOT NULL,
    tests TEXT NOT NULL,
    cycles INTEGER NOT NULL,
    graded INTEGER NOT NULL,
    init_dependent INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS wsp_detections (
    run_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    test TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    PRIMARY KEY (run_id, fault_id)
);
"""


def latest_run(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any] | None:
    found = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('wsp_runs', 'wsp_detections')"
    ).fetchone()[0]
    if found != 2:
        return None
    row = conn.execute(
        "SELECT * FROM wsp_runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    return None if row is None else {key: row[key] for key in row.keys()}


def record_run(
    conn: sqlite3.Connection,
    campaign_id: int,
    key: str,
    steps: Sequence[WspStep],
    grade: WspGrade,
) -> int:
    """Store one grade and its detections in one transaction."""
    conn.executescript(_SCHEMA)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO wsp_runs(campaign_id, run_key, program_digest, tests, cycles,
                                 graded, init_dependent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                key,
                program_digest(steps),
                json.dumps(program_tests(steps)),
                len(steps),
                grade.graded,
                len(grade.init_dependent),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO wsp_detections(run_id, fault_id, test, cycle) "
            "VALUES (?, ?, ?, ?)",
            [
                (run_id, fault_id, test, cycle)
                for fault_id, (test, cycle) in sorted(grade.detected.items())
            ],
        )
    return run_id


def report_block(
    conn: sqlite3.Connection, campaign_id: int, summary: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The coverage report's ``wsp`` block for the campaign's latest WSP grade, or
    None without one: what the program detected, and the scan's and its credit
    together over the scan's faults and those it left to the WSP."""
    run = latest_run(conn, campaign_id)
    if run is None:
        return None
    run_id = int(run["id"])
    eligible = "f.exclusion IN ('none', 'wsp') AND f.collapsed_into IS NULL"
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN {eligible} AND w.fault_id IS NOT NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN {eligible} AND (f.status = 'detected'
                    OR w.fault_id IS NOT NULL) THEN 1 ELSE 0 END),
          SUM(CASE WHEN {eligible} AND f.status = 'redundant'
                    AND w.fault_id IS NULL THEN 1 ELSE 0 END)
        FROM faults f
        LEFT JOIN wsp_detections w ON w.fault_id = f.id AND w.run_id = ?
        WHERE f.campaign_id = ?
        """,
        (run_id, campaign_id),
    ).fetchone()
    wsp_detected, detected, redundant = (int(v or 0) for v in row)
    by_test = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT test, COUNT(*) FROM wsp_detections WHERE run_id = ? GROUP BY test",
            (run_id,),
        )
    }
    tests = [str(t) for t in json.loads(str(run["tests"]))]
    structural = int(summary["structural_eligible"]) + int(
        summary.get("excluded_wsp", 0)
    )
    denominator = structural - redundant
    return {
        "program_digest": str(run["program_digest"]),
        "cycles": int(run["cycles"]),
        "tests": tests,
        "graded": int(run["graded"]),
        "detected": wsp_detected,
        "init_dependent": int(run["init_dependent"]),
        "detected_by_test": {t: by_test.get(t, 0) for t in tests},
        "combined": {
            "structural_eligible": structural,
            "denominator": denominator,
            "detected": detected,
            "redundant": redundant,
            "test_coverage_percent": (
                100.0 * detected / denominator if denominator else None
            ),
        },
    }


def wsp_lines(block: Mapping[str, Any]) -> list[str]:
    """The coverage report's text for its ``wsp`` block."""
    combined = block["combined"]
    percent = combined["test_coverage_percent"]
    return [
        f"wsp (integrity program, {block['cycles']} cycles, "
        f"{len(block['tests'])} tests):",
        f"  graded:              {block['graded']}",
        f"  detected:            {block['detected']}",
        f"  init_dependent:      {block['init_dependent']}",
        f"  combined coverage:   {combined['detected']} / {combined['denominator']} "
        + ("n/a" if percent is None else f"{percent:.3f}%"),
    ]


def _campaign(conn: sqlite3.Connection, campaign_type: str) -> int:
    from faultflow.db.campaign import latest_campaign_id

    campaign_id = latest_campaign_id(conn, campaign_type)
    if campaign_id is None:
        raise WspGradeError(f"no {campaign_type} campaign: run the scan test first")
    run = conn.execute(
        "SELECT completed_at FROM runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    if run is None or run["completed_at"] is None:
        raise WspGradeError(f"the {campaign_type} campaign has no completed run")
    return int(campaign_id)


def _program(
    cfg: FaultflowConfig,
    manifest: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    scan_outs: Mapping[int, str],
    path: Path | None,
) -> tuple[WspProgram, str]:
    """The program to grade with and where it came from: a file (``path``, else
    [wsp] program), warptap's, or faultflow's own builder ([wsp] builder)."""
    path = path or cfg.wsp.program
    if path is not None:
        text = path.read_text(encoding="utf-8")
        return WspProgram.from_text(text, wrapper, scan_outs), "file"
    if cfg.wsp.builder == "faultflow":
        return WspProgram.build(wrapper, scan_outs), "faultflow"
    from faultflow.wrap.wsp_icl import program_from_warptap

    top = str(manifest["top"])
    scan_ins = {
        int(chain["index"]): str(chain["scan_in"])
        for chain in manifest["chains"]
        if chain.get("kind") == "wrapper"
    }
    built = program_from_warptap(
        wrapper,
        scan_outs,
        top=top,
        scan_ins=scan_ins,
        scan_enable=str(manifest["scan_enable"]),
        margin=cfg.wsp.margin,
        exhaustive_opcodes=cfg.wsp.exhaustive_opcodes,
        icl_path=cfg.intermediate_dir / f"{top}_wsp.icl",
    )
    return built, "warptap"


def run_wsp(
    cfg: FaultflowConfig,
    *,
    force: bool = False,
    sim_threads: int | None = None,
    program: Path | None = None,
    export: Path | None = None,
) -> WspOutcome:
    """Grade, record and report (see the module docstring) the faults the latest
    scan campaign of the test mode ([testmode] mode) leaves to the WSP, with the
    program in the ``warptap-wsp-program`` file ``program`` ([wsp] program), else
    the one ``[wsp] builder`` makes: warptap's, built from the wrapper's ICL, or
    faultflow's own (faultflow.wrap.wsp_program). ``export``: write the program
    played as a file."""
    from faultflow.config import resolve_sim_threads
    from faultflow.db import connect, summary
    from faultflow.runner.runner import _load_core
    from faultflow.scan.cell_map import resolve_scan_cell_map

    if cfg.fault_model.model != "stuck_at":
        raise WspGradeError("ff.py wsp grades stuck-at faults only")
    core = _load_core()
    if core is None:
        raise WspGradeError("C++ extension _faultflow_core is required")
    if not cfg.scan_manifest_path.exists():
        raise WspGradeError("no scan manifest: run 'ff.py scan' and a scan test first")
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    wrapper = manifest.get("wrapper")
    if not isinstance(wrapper, dict) or wrapper.get("control") != "wsp":
        raise WspGradeError(
            "the scanned design has no IEEE 1500 WSP: wrap it with [wrap] control = "
            "wsp"
        )
    if "blocks" in wrapper:
        raise WspGradeError("ff.py wsp grades a block's WSP, not a SoC's chain")
    netlist = Path(str(manifest["generic_json"]))
    data = json.loads(netlist.read_text(encoding="utf-8"))
    module = data["modules"][str(manifest["top"])]
    cell_map = str(resolve_scan_cell_map(cfg))
    scan_outs = {
        int(chain["index"]): str(chain["scan_out"])
        for chain in manifest["chains"]
        if chain.get("kind") == "wrapper"
    }
    chosen, source = _program(cfg, manifest, wrapper, scan_outs, program)
    failures = conformance_failures(chosen, wrapper, scan_outs)
    if failures:
        raise WspGradeError(
            f"the WSP program disagrees with faultflow's WSP model at "
            f"{len(failures)} care bit(s); the first: {failures[0]}"
        )
    steps = chosen.steps
    if export is not None:
        export.write_text(
            json.dumps(chosen.to_json(wrapper, scan_outs), indent=2) + "\n",
            encoding="utf-8",
        )
    ports = {role: str(p["port"]) for role, p in wrapper["wsp"]["ports"].items()}
    inputs = [name for name, _ in port_bits(module, "input")]
    expected = compared(steps, ports["wso"])
    # The graded netlist holds every blackbox the scan campaign's does, but a
    # wrapper mode's own (the environment, the cores): the block itself.
    blackbox = list(cfg.blackbox_instances)
    benched = benches(
        core,
        str(netlist),
        cell_map,
        steps,
        inputs,
        ports,
        expected,
        unsupported=cfg.simulation.unsupported_cells,
        blackbox=blackbox,
    )
    start = time.perf_counter()
    golden_gate(benched, steps, expected)
    netlist_digest = hashlib.sha256(netlist.read_bytes()).hexdigest()
    key = hashlib.sha256(
        json.dumps(
            [program_digest(steps), netlist_digest, sorted(blackbox)], sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    campaign_type = "scan_extest" if cfg.test_mode == "extest" else "scan"
    with connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        campaign_id = _campaign(conn, campaign_type)
        previous = latest_run(conn, campaign_id)
        if previous is not None and previous["run_key"] == key and not force:
            totals = summary(conn, campaign_id=campaign_id)
            block = report_block(conn, campaign_id, totals)
            assert block is not None
            return WspOutcome(
                source=source,
                graded=int(previous["graded"]),
                detected=int(block["detected"]),
                init_dependent=int(previous["init_dependent"]),
                cycles=len(steps),
                seconds=time.perf_counter() - start,
                up_to_date=True,
                combined_coverage=block["combined"]["test_coverage_percent"],
            )
        rows_by_key = {
            str(row["site_key"]): int(row["compiled_net_index"])
            for row in core.list_site_keys(
                str(netlist), cell_map, cfg.simulation.unsupported_cells, blackbox
            )
        }
        faults: list[tuple[int, int, int]] = []
        for row in conn.execute(
            "SELECT id, fault_site_key, lower(fault_type) AS type FROM faults "
            "WHERE campaign_id = ? AND exclusion = 'wsp' AND collapsed_into IS NULL "
            "ORDER BY id",
            (campaign_id,),
        ):
            site = str(row["fault_site_key"])
            if site not in rows_by_key:
                raise WspGradeError(f"fault site {site} isn't in {netlist}")
            faults.append(
                (int(row["id"]), rows_by_key[site], int(row["type"] == "sa1"))
            )
    threads = resolve_sim_threads(
        sim_threads if sim_threads is not None else cfg.simulation.sim_threads
    )
    grade = grade_faults(benched, steps, faults, threads)
    with connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        record_run(conn, campaign_id, key, steps, grade)
        block = report_block(conn, campaign_id, summary(conn, campaign_id=campaign_id))
    assert block is not None
    report = cfg.intermediate_dir / "wsp_report.json"
    report.write_text(json.dumps(block, indent=2) + "\n", encoding="utf-8")
    return WspOutcome(
        source=source,
        graded=grade.graded,
        detected=len(grade.detected),
        init_dependent=len(grade.init_dependent),
        cycles=len(steps),
        seconds=time.perf_counter() - start,
        up_to_date=False,
        combined_coverage=block["combined"]["test_coverage_percent"],
    )
