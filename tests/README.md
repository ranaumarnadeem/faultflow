# FaultFlow Test Suite

This directory contains the complete test suite for faultflow: unit tests, integration tests, benchmarks, and pre-synthesized circuit fixtures.

## Directory Structure

```
tests/
├── benchmarks/           Pre-synthesized ISCAS-85 and ISCAS-89 circuits
│   ├── iscas85/          c17, c432, c499 (OSU035 + Sky130 synthesis)
│   └── iscas89/          s27 through s15850 (28 sequential circuits, Sky130)
│
├── cpp/                  C++17 engine tests (Catch2)
│   ├── atpg/             SAT ATPG tests (miter, CNF, compaction, transition)
│   ├── db/               SQLite campaign store tests
│   ├── fault/            Fault enumeration, collapsing, batch manager
│   ├── ir/               ParsedGraph, NormalizedGraph, CompiledSimGraph, WBR cells
│   ├── scan/             Scan chain, multi-clock stagger, scan sequences
│   ├── sim/              Bit-parallel engine, golden ref, gate eval, WBR modes
│   └── benchmarks/       C++ performance benchmarks (c17)
│
├── python/               Python layer tests (pytest)
│   ├── conftest.py       Shared fixtures: require_cpp_core, pytest markers
│   ├── campaign_fixtures.py  Campaign setup helpers (imported by test files)
│   ├── soc2_fixtures.py      SoC 2-block hierarchical test fixture data
│   ├── db_v3_helpers.py      Database schema helpers
│   │
│   ├── cli/              Shell and CLI command tests
│   ├── sim/              Core simulation flow tests
│   ├── atpg/             SAT ATPG algorithm tests (progressive, transition, compaction)
│   ├── scan/             Scan chain insertion, protocol, and scan ATPG tests
│   ├── wrap/             IEEE 1500 wrapper boundary cell tests (INTEST, EXTEST)
│   ├── project/          Hierarchical project aggregation and SoC retarget tests
│   │
│   └── (root-level)      Configuration, flow service, oracle, cell inventory tests
│
└── unit/                 Reserved for future unit tests
```

## Test Categories

### C++ Core Engine Tests (`tests/cpp/`)

C++ tests use **Catch2** and are organized by subsystem.

#### `atpg/`
SAT ATPG solver tests covering:
- Miter construction (fault-free vs. faulty CNF)
- SAT solver interface and timeout handling
- Compaction algorithms for test vector reduction
- Transition fault SAT queries
- Progressive ATPG loop with stalling detection

#### `db/`
SQLite campaign store tests:
- Schema initialization and version management
- Design fingerprint storage and retrieval
- Fault enumeration and persistence
- Fault detection batch writes
- Resume policy validation (fingerprint matching)

#### `fault/`
Fault model tests:
- Fault enumeration from NormalizedGraph nets
- Equivalence and dominance collapsing
- Stuck-at (SA0/SA1) fault generation
- Fanout-branch checkpoint splitting
- Batch manager and fault effect polymorphism

#### `ir/`
Graph IR tests:
- ParsedGraph: Yosys JSON parsing and net ID preservation
- NormalizedGraph: cell map lookup, aliasing, levelization, clock/reset tagging
- CompiledSimGraph: compilation to flat arrays, YosysNetID↔CompiledNetIndex mapping
- WBR cells: wrapper boundary register insertion and lowering

#### `scan/`
Sequential and scan chain tests:
- Scan FF identification and chain ordering
- Shift operations and data flow
- Multi-clock domain stagger and synchronization
- Scan capture/shift protocol verification
- Scan sequence generation and execution

#### `sim/`
Simulation engine tests:
- Bit-parallel gate evaluation (all 30+ gate types)
- Golden reference simulator (scalar correctness baseline)
- Fault injection at net level (SA0/SA1 masking)
- Fault-free vs. faulty universe tracking
- Multi-output cell lowering (ADDF, FF+QN, fanout branches)
- WBR shift and normal-mode operation

#### `benchmarks/`
Performance benchmarks (Google Benchmark):
- c17 combinational simulation throughput
- Fault enumeration and injection overhead
- Compilation latency

### Python Layer Tests (`tests/python/`)

Python tests use **pytest** and are organized by subsystem and flow stage.

#### Pytest Markers

Available markers for test organization and selective execution:

- `unit` — fast, no external tools required (simulated/mocked)
- `golden` — requires GoldenRefSim C++ extension, no external tools
- `integration` — requires external tools (Yosys, iverilog, verilator, Quaigh)
- `slow` — benchmark circuits or long-running operations (>30 seconds)
- `verification` — vector verification gate tests (iverilog)
- `sequential` — sequential circuit feature tests (Phase 2.5+)

#### `cli/`
Shell and CLI command tests:
- Command-line argument parsing and validation
- Subcommand flow (init, sim, status, run)
- Config file loading and inheritance
- Error handling and user feedback

#### `sim/`
Core simulation flow tests:
- Vector application and combinational evaluation
- Fault batch processing and detection
- Observable set tracking (PIs, POs, test points)
- Cycle-based simulation for sequential circuits
- Vector source attribution (Quaigh, ATPG, random)

#### `atpg/`
SAT ATPG algorithm tests:
- Progressive fault list reduction
- Transition fault ATPG queries
- Test vector compaction
- ATPG timeout and unknown fault handling
- Redundancy classification persistence
- Comparison with Quaigh ATPG (when available)

#### `scan/`
Scan chain insertion and protocol tests:
- Scan chain discovery and ordering
- INTEST wrapper boundary fusion
- EXTEST wrapper with tri-state control
- Scan sequence generation (shift, capture, parallel load)
- Scan ATPG and coverage integration
- Multi-clock domain test isolation

#### `wrap/`
IEEE 1500 wrapper boundary cell tests:
- Boundary register cells (IN, OUT, INOUT)
- Wrapper operation modes (INTEST, EXTEST, normal)
- Parallel and serial data paths
- Tri-state enable/disable control
- Wrapper port observation and control

#### `project/`
Hierarchical project aggregation and SoC retarget tests:
- Block-as-top INTEST simulation
- Assembly-level EXTEST fault simulation
- Owning-role and handoff rules
- Block-to-block boundary synchronization
- Coverage aggregation across blocks
- SoC retargeting from nominal to real blocks

#### Root-level tests
Configuration, flow service, and orchestration tests:
- Configuration parsing (`config.ofs`)
- Flow service orchestration and state management
- Oracle response generation
- Cell inventory and library selection
- Test vector I/O (YAML, JSON)

### Benchmark Circuits (`tests/benchmarks/`)

Pre-synthesized reference circuits for regression testing and performance evaluation.

#### `iscas85/`
Combinational circuits (ISCAS 1985):
- **c17** (5 inputs, 11 gates) — minimal combinational test
- **c432** (36 inputs, 162 gates) — small-scale logic
- **c499** (41 inputs, 202 gates) — moderate-scale logic

Available in dual PDK variants:
- OSU035 (legacy reference)
- Sky130 HD (current production target)

#### `iscas89/`
Sequential circuits (ISCAS 1989):
- **s27, s298, s344, s349, ...** through **s15850** (28 circuits total)
- Range: 10 PIs / 1 FF to 611 PIs / 597 FFs
- Includes scan chains and sequential behavior
- Sky130 synthesis only

These circuits are used for:
- Phase 2.5+ sequential feature validation
- Multi-clock domain testing (s38417, s38584 multi-clock)
- Transition fault benchmarking
- DFT rule checking
- SoC hierarchical project aggregation (when grouped into blocks)

## Running Tests

### Prerequisites

Build the C++ extension first (required for all tests):

```bash
cd C:\Users\Potato\Desktop\faultflow
cmake -S . -B build -G Ninja
cmake --build build -- -j4
```

All dev tool commands must run in WSL (see project CLAUDE.md for WSL-only policy).

### Python Tests

Run all tests (unit + integration):

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && pytest tests/python/ -v"
```

Run only fast tests (skip slow benchmarks):

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && pytest tests/python/ -m 'not slow' -v"
```

Run a specific test subdirectory:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && pytest tests/python/scan/ -v"
```

Run tests matching a pattern:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && pytest tests/python/ -k 'collapsing' -v"
```

Run with coverage report:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && pytest tests/python/ --cov=faultflow --cov-report=html"
```

### C++ Tests

Run all C++ tests (Catch2 via CTest):

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && ctest --test-dir build --output-on-failure -j4"
```

Run a specific C++ test suite:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && ctest --test-dir build -R 'SimEngine' --output-on-failure"
```

Run C++ benchmarks:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && ./build/tests/cpp/benchmarks/benchmark_sim --benchmark_min_time=1"
```

### Linting and Type Checking

Format and lint all Python code:

```bash
wsl -e bash -c "cd /mnt/c/Users/Potato/Desktop/faultflow && source venv/bin/activate && black . && flake8 . && mypy ."
```

## Test File Naming

### Python Tests

- Standard test files: `test_<subsystem>_<feature>.py`
  - Examples: `test_sim_cycle_based.py`, `test_scan_atpg_pipeline.py`, `test_project_blocks.py`

- Fixture and helper modules (no `test_` prefix):
  - `conftest.py` — pytest configuration, shared fixtures, marker definitions
  - `campaign_fixtures.py` — campaign and flow setup helpers
  - `soc2_fixtures.py` — SoC 2-block hierarchical test data
  - `db_v3_helpers.py` — database schema and initialization helpers

### C++ Tests

- Test files: `test_<component>.cpp`
- Located in the matching subsystem directory (e.g., `tests/cpp/sim/test_gate_eval.cpp`)

## Adding New Tests

### Adding Python Tests

1. **Locate the correct subdirectory** under `tests/python/`:
   - For CLI tests: `tests/python/cli/`
   - For simulation logic: `tests/python/sim/`
   - For SAT ATPG: `tests/python/atpg/`
   - For new features: create a matching subdirectory

2. **Create or update the test file**:
   - Name: `test_<feature>.py`
   - Mark with appropriate pytest markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow`
   - Import shared fixtures from `conftest.py` or subsystem-specific helpers

3. **Example test structure**:

   ```python
   import pytest
   from faultflow.sim import SimEngine
   from campaign_fixtures import setup_c17_campaign  # pytest adds tests/python/ to sys.path

   @pytest.mark.unit
   def test_gate_eval_and2(require_cpp_core):
       """Test AND2 gate evaluation in bit-parallel engine."""
       # Test body
       pass

   @pytest.mark.integration
   def test_sim_with_external_vectors():
       """Test simulation with Quaigh-generated vectors."""
       campaign = setup_c17_campaign()
       # Test body
       pass
   ```

4. **Update test markers** in `conftest.py` if introducing a new test category.

### Adding C++ Tests

1. **Create a test file** in the appropriate subsystem directory under `tests/cpp/`:
   - `tests/cpp/sim/test_new_feature.cpp`
   - `tests/cpp/atpg/test_solver_timeout.cpp`

2. **Use Catch2 framework**:

   ```cpp
   #include <catch2/catch_test_macros.hpp>
   #include "faultflow/core/sim/engine.h"

   TEST_CASE("SimEngine::evaluate_and2", "[sim][gate]") {
       // Test body
       REQUIRE(result == expected);
   }
   ```

3. **Register in CMakeLists.txt**:
   - Add to the appropriate `add_executable()` or use `add_test()` for CTest integration

4. **Run and verify**:
   ```bash
   ctest --test-dir build -R 'new_feature' --output-on-failure
   ```

## Test Dependencies

### External Tools (Integration Tests)

- **Yosys** 0.61+ — synthesis and netlist generation
- **iverilog** — vector verification gate
- **verilator** (optional) — faster verification
- **Quaigh** (optional) — ATPG reference and BENCH format comparison
- **nl2bench** (optional) — Verilog-to-BENCH conversion for Quaigh

### Python Packages

Listed in project dependencies; installed via `pip install -e .` in the venv:
- pytest, pytest-cov — test runner and coverage
- click — CLI testing
- pyyaml — config and vector I/O
- rich — output formatting
- jsonschema — schema validation

### C++ Libraries

Linked by CMake; headers in `src/core/`:
- nlohmann/json — Yosys JSON parsing
- SQLiteCpp — campaign store
- spdlog — logging
- Catch2 — unit test framework
- Google Benchmark — performance benchmarks

## Test Best Practices

### Isolation

- Each test should be independent; avoid shared mutable state
- Use fixtures to set up clean state (via `conftest.py`)
- Clean up artifacts (databases, temp files) in teardown

### Clarity

- Use descriptive test names: `test_<what>_<when>_<expected_result>`
- Include docstrings explaining the test purpose
- Keep individual tests focused on one concern

### Performance

- Mark slow tests (>30 seconds) with `@pytest.mark.slow` so they can be skipped in CI
- Use C++ Golden Reference Simulator for correctness; only verify against external tools when necessary
- Pre-synthesize benchmark circuits; never synthesize on-the-fly in tests

### Coverage

- Aim for 80%+ line coverage on critical paths (sim engine, fault enumeration, batch manager)
- Use `pytest --cov` to identify untested code paths
- Add tests when fixing bugs to prevent regression

## CI/CD Integration

Tests are run on each commit via GitHub Actions (see `.github/workflows/`):

- **Quick checks** (unit tests, no external tools) — run on every PR
- **Integration tests** — run nightly or on manual trigger
- **Benchmarks** — track performance trends

The CI environment has Yosys, iverilog, and Quaigh pre-installed; local development should match this setup.
