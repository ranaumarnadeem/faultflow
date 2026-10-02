# faultflow

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-github%20pages-2ea44f)](https://ranaumarnadeem.github.io/faultflow/)
[![CI](https://github.com/ranaumarnadeem/faultflow/actions/workflows/nix.yml/badge.svg)](https://github.com/ranaumarnadeem/faultflow/actions/workflows/nix.yml)

**faultflow is an open-source Automatic Test Pattern Generation (ATPG) and fault simulation engine for post-synthesis gate-level netlists from Yosys, with SAT-based test generation, stuck-at and transition fault models, scan insertion and IEEE 1500 core wrapping.**

Manufacturing test of digital chips depends on ATPG, and the established tools (Synopsys TetraMAX/TestMAX, Cadence Modus, Siemens Tessent) are commercial. faultflow is a design-for-test (DFT) engine for open flows: it reads a Yosys-synthesized netlist, inserts scan, generates and compacts test patterns with a CaDiCaL SAT solver, and reports fault coverage. It runs from an interactive Tcl shell (`python3 ff.py shell`) or a batch CLI (`python3 ff.py <command>`).

- Documentation: https://ranaumarnadeem.github.io/faultflow/
- Benchmarks: [docs/benchmarking.md](docs/benchmarking.md)
- Engine: C++17 core, Python bridge (pybind11), CaDiCaL SAT solver

## Features

- **Native SAT ATPG** (CaDiCaL) for combinational and full-scan designs — cone-of-influence
  CNF, escalating per-fault timeouts, parallel workers, sim-verified compaction.
- **Stuck-at and transition faults** — transition as combinational broadside plus scan LOC and LOS.
- **Full-scan insertion** with structural chain validation and scan ATPG on a reduced pseudo-PI/PO view.
- **DFT rule check** (`rule_check`) — structural scan/clock design rules.
- **IEEE-1500 wrapper (WBR)** — INTEST (core) and EXTEST (interconnect) boundary test.
- **Hierarchical SoC aggregation** — per-block INTEST + assembly EXTEST rolled into one chip number.
- **Scan-pattern retargeting** — remap a block's scan patterns onto a SoC scan path.
- **Multi-clock** — domain-aware protocol, per-domain at-speed, cross-domain paths masked.
- **Fault collapsing** — equivalence-based (incl. compound AOI/OAI cells).
- **PDKs** — Sky130 HD (default) and OSU035; Yosys front end, optional iverilog verification.

## Results

Stuck-at results, copied from [docs/benchmarking.md](docs/benchmarking.md). Hybrid ATPG is the default flow (random fill, then SAT ATPG on the faults left); Random runs never call SAT.

| Design | Fault model | ATPG mode | FFs | Scan chains | Test coverage | Patterns | Wall time |
|---|---|---|---:|---:|---:|---:|---:|
| s5378 | Stuck-at | Hybrid | 162 | 17 | 100.00% | 577 | 994.2 s |
| s9234_1 | Stuck-at | Hybrid | 135 | 14 | 100.00% | 505 | 769.3 s |
| s15850 | Stuck-at | Hybrid | 559 | 56 | 100.00% | 866 | 2,654.3 s |
| boxcar | Stuck-at | Hybrid | 1,130 | 113 | 100.00% | 1,100 | 1,243.3 s |
| boxcar | Stuck-at | Random | 1,130 | 113 | 98.926% | 288 | 336.1 s |
| picorv32a | Stuck-at | Hybrid | 1,613 | 162 | 99.969% | 2,441 | 13,044.3 s |
| picorv32a | Stuck-at | Random | 1,613 | 162 | 99.785% | 1,164 | 4,387.8 s |

Test coverage is detected faults over in-scope faults minus those SAT proved redundant; fault coverage counts redundant faults as undetected. Random runs prove none redundant, so for them the two are equal. The picorv32a Hybrid row is the most recent re-run (fault collapsing on); its FF count is the 1,613-cell scan length in the SoC1 table. Full tables, including every ISCAS-85/89 design, collapsing on and off, and hierarchical SoC roll-ups: [docs/benchmarking.md](docs/benchmarking.md).

### Transition delay faults

| Design | ATPG mode | FFs | Scan chains | Test coverage | Fault coverage | Wall time |
|---|---|---:|---:|---:|---:|---:|
| s9234_1 | Hybrid | 135 | 14 | 99.91% | 86.06% | 834.4 s |
| boxcar¹ | Hybrid | 1,130 | 113 | 100.00% | 80.98% | ~11.7 h |
| picorv32a | Hybrid | 1,613 | 162 | 94.73% | 89.81% | ~10.5 h |

Pattern counts are not recorded for these runs.

¹ Restarted mid-run after diagnosing a WSL 9P-bridge I/O bottleneck: the SQLite DB had grown large on `/mnt/c` and was moved to native ext4 storage, keeping all prior progress. The run fully converged (`atpg_terminal=COMPLETE`, 0 timeouts, 0 unknowns) and was stopped after all 27,988 faults resolved (0 undetected), once a subsequent vector-compaction phase began growing the DB unboundedly (29 GB in <1 h) with no further effect on the coverage numerator or denominator.

## How faultflow compares

| | faultflow | Fault (AUCOHL) | Atalanta | Commercial (TetraMAX/TestMAX, Modus, Tessent) |
|---|---|---|---|---|
| License | Apache-2.0 | Apache-2.0 | Academic | Proprietary |
| Input | Yosys gate-level netlist | Gate-level Verilog netlist | ISCAS bench | Industry netlists |
| Test generation | SAT (CaDiCaL) + random | Random/LFSR + Quaigh, Atalanta or PODEM | FAN | Proprietary |
| Stuck-at | Yes | Yes | Yes | Yes |
| Transition (LOC/LOS) | Yes | No | No | Yes; LOC/LOS: Unverified |
| Scan insertion | Yes | Yes | No | Yes |
| IEEE 1500 wrapper | Yes | No | No | Yes |
| Hierarchical SoC roll-up | Yes | No | No | Tessent: Yes; TestMAX, Modus: Unverified |
| Scan compression | Yes | No | No | Yes |

Fault and Atalanta were checked against their source and documentation, and the commercial tools against vendor product pages and datasheets (October 2026); Unverified marks what public sources do not confirm. Fault inserts an IEEE 1149.1 JTAG TAP, which is not an IEEE 1500 wrapper. The commercial column covers each vendor's DFT product family: scan insertion, compression and core wrapping ship as companion tools to the ATPG engine.

## Quick start

```bash
python3 ff.py init --top <top> -c config.ofs
python3 ff.py sim --top <top> -c config.ofs
python3 ff.py status --top <top> -c config.ofs
```

Native SAT ATPG is the default combinational flow when `[atpg] tool = native` in
`config.ofs`. See [`config.ofs.example`](config.ofs.example) for a full template.

External vectors bypass native ATPG:

```bash
python3 ff.py sim --top <top> --ext vectors.test
```

`vectors.test` requires a same-stem `vectors.bench` sidecar for PI order.

To reset internal workspace state (keeps deliverables in `output/<top>/`):

```bash
python3 ff.py sim --top <top> --clean
```

## More flows

Beyond `init` / `sim` / `status`, the CLI also covers IEEE-1500 wrapper test
modes, hierarchical SoC aggregation, and scan-pattern retargeting:

```bash
python3 ff.py intest   --top <top> -c config.ofs     # IEEE-1500 INTEST (core)
python3 ff.py extest   --top <top> -c config.ofs     # IEEE-1500 EXTEST (interconnect)
python3 ff.py project  -p project.json                # per-block INTEST + assembly EXTEST -> one chip number
python3 ff.py retarget --patterns p.json --soc-access a.json --block blkA --out out.json
python3 ff.py add-clock clk -c config.ofs             # declare a clock domain in config.ofs
```

Or drive any of these interactively instead:

```bash
python3 ff.py shell
```

See the [full documentation](https://ranaumarnadeem.github.io/faultflow/) for the
complete command reference and worked examples.

## Output layout

```
output/<top>/
├── <top>_scan.v          # scan deliverable (after scan + techmap)
├── <top>_scan.json       # scanned generic JSON
├── scan.rpt
├── coverage.rpt          # human-readable coverage report
├── patterns.test         # ATPG patterns (when generated)
└── .faultflow/           # internal workspace (removed by --clean)
    ├── faultflow.sqlite  # unified comb + scan campaigns
    ├── logs/
    ├── manifests/
    ├── intermediate/
    ├── verification/
    └── generated_scripts/
```

Synth JSON for ISCAS benchmarks is read from `tests/benchmarks/iscas85/synth/` and
`tests/benchmarks/iscas89/synth/`. Default PDK is Sky130 HD.

Comb and scan campaigns share `output/<top>/.faultflow/faultflow.sqlite`. Scan ATPG:

```bash
python3 ff.py scan --top <top> -c config.ofs
python3 ff.py scan-check --top <top> -c config.ofs
python3 ff.py sim --scan --top <top> -c config.ofs
```

## Build

```bash
cmake -S . -B build -G Ninja
cmake --build build -- -j2
```

## Tests

```bash
ctest --test-dir build --output-on-failure
PYTHONPATH=. venv/bin/pytest tests/python -q
```

## Nix

A flake (`flake.nix` + `nix/`) packages the above: `nix develop` for the full
toolchain, `nix build` for a standalone `faultflow` binary, `nix flake check`
to run both full test suites fully hermetically. See
[`docs/getting_started/installation.md`](docs/getting_started/installation.md#building-with-nix)
for details.
