import json
import os
import tempfile
import pathlib
import shutil

from faultflow.config import load_config
from faultflow.runner import Runner

ROOT = pathlib.Path("/mnt/c/Users/Potato/Desktop/faultflow")
CELL = ROOT / "cells/osu/osu035.json"
CIRCUITS = [
    ("c17", ROOT / "tests/benchmarks/iscas85/synth/c17.json"),
    ("c432", ROOT / "tests/benchmarks/iscas85/synth/c432.json"),
    ("c499", ROOT / "tests/benchmarks/iscas85/synth/c499.json"),
]


def run(top, netlist, model, workdir):
    cfg_path = workdir / "config.ofs"
    cfg_path.write_text(
        f"""
[design]
netlist = {netlist}
cell_lib = {CELL}
[fault_model]
model = {model}
[simulation]
unsupported_cells = fail
[atpg]
random_vectors = 64
max_rounds = 20
sat_timeout_seconds = 10
compaction = none
[report]
threshold = 100.0
""".strip() + "\n",
        encoding="utf-8",
    )
    r = Runner(load_config(cfg_path, top))
    r.init()
    r.sim(clean=True, max_rounds=20, target_coverage=100.0)
    rep = json.loads(
        (
            pathlib.Path("output")
            / top
            / ".faultflow/intermediate/coverage_report.json"
        ).read_text()
    )
    return rep["summary"], rep["run"]


rows = []
for top, nl in CIRCUITS:
    if not nl.exists():
        continue
    for model in ("stuck_at", "transition"):
        d = pathlib.Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(d)
        try:
            shutil.copytree(ROOT / "schemas", d / "schemas")
            s, run_ = run(top, nl, model, d)
            rows.append(
                (
                    top,
                    model,
                    s["total_raw_faults"],
                    s["denominator"],
                    s["detected"],
                    s["redundant"],
                    s.get("undetected", 0),
                    round(float(s["coverage_percent"] or 0), 2),
                    run_["vector_source"],
                    run_["atpg_terminal_reason"],
                    run_["vector_count"],
                )
            )
        finally:
            os.chdir(cwd)
            shutil.rmtree(d, ignore_errors=True)

print(
    "CIRCUIT|MODEL|total_raw|denom|detected|redundant|undet|cov%|source|terminal|vecs"
)
for r in rows:
    print("|".join(str(x) for x in r))
