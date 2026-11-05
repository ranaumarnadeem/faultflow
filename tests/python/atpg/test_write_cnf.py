"""`ff.py write-cnf`: one fault's ATPG CNF as DIMACS, with its variable map.

Each written file is checked here by a small DPLL, independent of the C++
core: its satisfiability is the verdict the map records, and on a SAT file the
assignment it finds, read through the map's input variables, detects the fault
in the core's fault simulator."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
LIBERTY = ROOT / "cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

RTL = """module foo(input a, input b, input c, input d, output y1, output y2);
  assign y1 = (a | b) & c;
  assign y2 = (a ^ d) | b;
endmodule
"""


@pytest.fixture
def flow_tools(require_cpp_core: None) -> None:
    if shutil.which("yosys") is None:
        pytest.skip("needs yosys")


def _dpll(clauses: list[list[int]]) -> dict[int, bool] | None:
    """A model of `clauses`, or None: unit propagation, then branching."""

    def solve(assign: dict[int, bool]) -> dict[int, bool] | None:
        assign = dict(assign)
        while True:
            unit = None
            for clause in clauses:
                free = []
                for lit in clause:
                    value = assign.get(abs(lit))
                    if value is None:
                        free.append(lit)
                    elif value == (lit > 0):
                        break
                else:
                    if not free:
                        return None
                    if len(free) == 1:
                        unit = free[0]
                        break
            if unit is None:
                break
            assign[abs(unit)] = unit > 0
        for clause in clauses:
            for lit in clause:
                if abs(lit) not in assign:
                    for value in (True, False):
                        found = solve({**assign, abs(lit): value})
                        if found is not None:
                            return found
                    return None
        return assign

    return solve({})


def _read_dimacs(path: Path) -> tuple[int, list[list[int]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split()
    assert header[:2] == ["p", "cnf"]
    clauses = []
    for line in lines[1:]:
        lits = [int(t) for t in line.split()]
        assert lits[-1] == 0
        clauses.append(lits[:-1])
    assert len(clauses) == int(header[3])
    return int(header[2]), clauses


def _flow(work: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> Any:
    from faultflow.cli import main

    monkeypatch.chdir(work)
    (work / "foo.v").write_text(RTL, encoding="utf-8")
    ofs = work / "foo.ofs"
    ofs.write_text(
        f"[design]\nnetlist = {work / 'foo.v'}\ncell_lib = {CELL_MAP}\n"
        f"liberty = {LIBERTY}\n\n"
        f"[fault_model]\nmodel = {model}\ncollapsing = false\n\n"
        "[simulation]\nverify = false\n\n"
        "[atpg]\nrandom_vectors = 8\ncompaction = none\n",
        encoding="utf-8",
    )
    for step in (["init"], ["sim"]):
        assert main([*step, "--top", "foo", "-c", str(ofs)]) == 0, step
    return load_config(ofs, "foo")


def _sites(cfg: Any) -> list[tuple[str, str]]:
    conn = sqlite3.connect(cfg.db_path)
    try:
        return [
            (str(key), str(ftype))
            for key, ftype in conn.execute(
                "SELECT fault_site_key, fault_type FROM faults "
                "WHERE exclusion = 'none' ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _detects(cfg: Any, site: str, ftype: str, vectors: list[dict[str, bool]]) -> bool:
    """Whether the core's fault simulator detects the fault with `vectors` (one
    vector, or a launch/capture pair) -- graded in a scratch copy of the DB."""
    from faultflow.runner.runner import _load_core

    core = _load_core()
    assert core is not None
    db = Path(str(cfg.db_path) + ".check")
    shutil.copyfile(cfg.db_path, db)
    conn = sqlite3.connect(db)
    try:
        (fault_id,) = conn.execute(
            "SELECT id FROM faults WHERE fault_site_key = ? AND fault_type = ?",
            (site, ftype),
        ).fetchone()
        conn.execute("UPDATE faults SET status = 'undetected'")
        conn.commit()
    finally:
        conn.close()
    netlist = cfg.intermediate_dir / "foo.json"
    if len(vectors) == 2:
        return bool(
            core.verify_transition_candidate(
                str(netlist), str(cfg.cell_lib), str(db), fault_id, *vectors
            )
        )
    return bool(
        core.verify_fault_candidate(
            str(netlist), str(cfg.cell_lib), str(db), fault_id, vectors[0]
        )
    )


@pytest.mark.parametrize("model", ["stuck_at", "transition"])
def test_written_cnf_matches_its_verdict_and_tests_the_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None, model: str
) -> None:
    from faultflow.cli import main

    cfg = _flow(tmp_path, monkeypatch, model)
    sites = _sites(cfg)
    assert sites
    sat = 0
    for site, ftype in sites:
        out = tmp_path / "cnf" / f"{len(list(tmp_path.glob('cnf/*')))}.cnf"
        argv = ["write-cnf", "--top", "foo", "-c", str(tmp_path / "foo.ofs")]
        assert main([*argv, "--site", site, "--type", ftype, "-o", str(out)]) == 0
        cnf_map = json.loads(Path(str(out) + ".json").read_text(encoding="utf-8"))
        assert cnf_map["format"] == "faultflow-fault-cnf"
        assert cnf_map["fault"] == {
            **cnf_map["fault"],
            "site_key": site,
            "type": ftype,
            "model": model,
        }
        max_var, clauses = _read_dimacs(out)
        model_found = _dpll(clauses)
        assert (model_found is not None) == (cnf_map["result"] == "SAT"), site
        if model_found is None:
            continue
        sat += 1
        assert all(0 < v <= max_var for v in cnf_map["inputs"].values())
        assert model_found.get(cnf_map["fault_var"]) == (ftype == "sa1")

        def vector(frame: dict[str, int]) -> dict[str, bool]:
            return {name: model_found.get(var, False) for name, var in frame.items()}

        frames = [vector(cnf_map["inputs"])]
        if model == "transition":
            assert set(cnf_map["launch_inputs"]) == set(cnf_map["inputs"])
            frames.insert(0, vector(cnf_map["launch_inputs"]))
        else:
            assert cnf_map["launch_inputs"] == {}
        assert _detects(cfg, site, ftype, frames), (site, ftype)
    assert sat > 0


def test_write_cnf_names_the_sites_of_an_ambiguous_net(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow_tools: None
) -> None:
    from faultflow.cli import main

    _flow(tmp_path, monkeypatch, "stuck_at")
    # b fans out to both outputs: its stem and its branches are sites.
    argv = ["write-cnf", "--top", "foo", "-c", str(tmp_path / "foo.ofs")]
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--site", "b", "--type", "sa0", "-o", str(tmp_path / "b.cnf")])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main([*argv, "--site", "nope", "--type", "sa0", "-o", str(tmp_path / "n.cnf")])
