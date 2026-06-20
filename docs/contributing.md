# Contributing

Contributions are welcome. This page covers how to build, test, and lint, plus the
conventions the codebase holds to.

```{note}
faultflow develops and builds on **Unix-like systems only** (Linux or WSL2). See
[Installation](getting_started/installation.md).
```

## Repository layout

```text
faultflow/
├── src/core/        C++17 engine
│   ├── ir/           ParsedGraph -> NormalizedGraph -> CompiledSimGraph
│   ├── fault/        enumeration, collapsing, runtime fault records
│   ├── sim/          bit-parallel engine, golden reference, gate eval, state
│   ├── atpg/         native SAT ATPG (CaDiCaL)
│   ├── scan/         scan-chain extraction and pattern simulation
│   ├── db/           SQLite campaign store
│   └── bindings/     pybind11 module (_faultflow_core)
├── faultflow/       Python control plane (CLI, runner, reporter, shell, verify)
├── cells/           OSU035 and Sky130 JSON cell maps + Liberty/models
├── schemas/         JSON schemas for the cell map, coverage, oracle response
├── tests/           C++ (Catch2) and Python (pytest); benchmark circuits
├── examples/        runnable example designs
└── docs/            this documentation
```

## Build and test

```bash
# C++ core + tests + the Python extension module
cmake -S . -B build -G Ninja
cmake --build build -- -j2

# C++ unit tests
ctest --test-dir build --output-on-failure

# Python deps + tests (in a virtual environment)
pip install -r requirements-dev.txt
PYTHONPATH=. venv/bin/pytest tests/python -q
```

The C++ core requires CaDiCaL, SQLiteCpp, and pybind11 to be available; see
[Installation](getting_started/installation.md).

## Python style

All Python must pass three tools, configured by `.flake8` (line length 88):

```bash
black .          # formatter
flake8 .         # linter
mypy .           # type checker
```

Run them before opening a pull request.

## Test-first convention

The project follows a test-driven workflow: behavior is specified by a failing test
first, then implemented until the test passes. Every bit-parallel simulation result is
cross-checked against the scalar golden-reference simulator — if they disagree, the
bit-parallel engine is considered wrong. New simulation or ATPG features should come
with a golden-reference comparison.

## Design rules to respect

A few invariants run deep in the architecture (see the
[Architecture overview](architecture/overview.md)). Changes that violate them will not
hold up:

- **Keep the IR layers separate.** `ParsedGraph` and `NormalizedGraph` may use maps and
  strings; `CompiledSimGraph` and `SimState` are flat arrays only. Do not leak maps or
  strings into the compiled graph or the hot loop.
- **The compiled graph is immutable.** All mutable simulation state lives in `SimState`.
- **Two net-ID spaces are distinct.** The sparse Yosys net IDs and the dense compiled
  indices are never interchanged.
- **No virtual calls or branches in the simulation hot loop.** Gate evaluation is a flat
  bitwise table.
- **One output per `SimNode`.** Multi-output cells are lowered during compilation.
- **The core stays binary.** No X/Z handling in the current engine — that is a
  deliberate, roadmapped change (see below).
- **Never silently skip an unsupported cell.** Honor the `fail` / `blackbox` policy.
- **Exclusions are tagged, not dropped.** Every fault is enumerated and counted; clock,
  reset, and blackbox faults are tagged and reported separately.

The C++ core reads cell semantics from the JSON cell map only — do not add a Liberty
parser to the core.

## Building the documentation

The docs are Sphinx + Furo + MyST-Markdown. From the repo root:

```bash
source venv/bin/activate
pip install -r docs/requirements.txt
sphinx-build -b html docs docs/_build/html
# open docs/_build/html/index.html
```

Or, from `docs/`, `make html`. The site is published to GitHub Pages automatically by
`.github/workflows/docs.yml` on pushes to `main`. The internal planning notes under
`docs/plans/` (including `docs/plans/papers/`) are intentionally excluded from the build.

## Where the deep specs live

The durable architecture reference is `.claude/CLAUDE.md`, and per-phase build specs and
test specs live under `.claude/phaseN/`. The authoritative forward plan is
`docs/plans/roadmap.md`, summarized on the [Roadmap](roadmap.md) page.
