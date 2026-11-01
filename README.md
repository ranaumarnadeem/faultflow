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

Selected rows from [docs/benchmarking.md](docs/benchmarking.md). Hybrid ATPG is the default flow (random fill, then SAT ATPG on the faults left); Random runs never call SAT.

| Design | Fault model | ATPG mode | FFs | Scan chains | Test coverage | Patterns | Wall time |
|---|---|---|---:|---:|---:|---:|---:|
| s5378 | Stuck-at | Hybrid | 162 | 17 | 100.00% | 427 | 145 s |
| s9234_1 | Stuck-at | Hybrid | 135 | 14 | 100.00% | 226 | 39 s |
| s13207 | Stuck-at | Hybrid | 225 | 64 | 100.00% | 261 | 148 s |
| s15850 | Stuck-at | Hybrid | 157 | 56 | 100.00% | 213 | 115 s |
| genericfir_small | Stuck-at | Hybrid | 471 | 48 | 100.00% | 406 | 107 s |
| boxcar¹ | Stuck-at | Hybrid | 1,130 | 113 | 100.00% | 1,100 | 1,243.3 s |
| boxcar¹ | Stuck-at | Random | 1,130 | 113 | 98.926% | not recorded | 336.1 s |
| picorv32a | Stuck-at | Hybrid | 1,613 | 162 | 99.992% | 2,434 | 2,444 s |
| picorv32a¹ | Stuck-at | Random | 1,613 | 162 | 99.785% | not recorded | 4,387.8 s |

Hybrid reaches 100.00% stuck-at test coverage on 32 of the 33 designs in [docs/benchmarking.md](docs/benchmarking.md) (mean 99.99976%); picorv32a is at 99.992%. Test coverage is detected faults over in-scope faults minus those SAT proved redundant; fault coverage counts redundant faults as undetected. Random runs prove none redundant, so for them the two are equal. Fault collapsing is off in every run, and each run uses one worker. Every fault a scan campaign credited was replayed with its exported patterns through the full scan-protocol fault simulator; none failed to reproduce. All runs were on one laptop (Intel Core Ultra 5 125U) in WSL2 Ubuntu 24.04 with 10 logical CPUs and 23 GB of RAM. Full tables, including every ISCAS-85/89 design and hierarchical SoC roll-ups: [docs/benchmarking.md](docs/benchmarking.md).

### Transition delay faults

Launch-on-capture, Hybrid ATPG.

| Design | FFs | Scan chains | Test coverage | Fault coverage | Wall time |
|---|---:|---:|---:|---:|---:|
| s5378 | 162 | 17 | 99.38% | 71.70% | 162 s |
| s9234_1 | 135 | 14 | 99.65% | 86.84% | 74 s |
| s13207 | 225 | 64 | 99.73% | 77.80% | 121 s |
| s15850 | 157 | 56 | 98.40% | 70.11% | 85 s |
| genericfir_small | 471 | 48 | 99.80% | 85.41% | 985 s |
| boxcar¹ | 1,130 | 113 | 100.00% | 80.98% | ~11.7 h |
| picorv32a¹ ² | 1,613 | 162 | 94.73% | 89.81% | ~10.5 h |

Against Fault 0.9.4's default pseudo-random ATPG on 16 ISCAS netlists mapped to OSU035, faultflow took 239.2 s in total against 5,150.3 s (21.5x; geometric mean 14.7x); Fault with Quaigh as its ATPG took 111.6 s. Setup, logs and scripts: [docs/benchmarks/fault_compare](docs/benchmarks/fault_compare/README.md).

¹ Earlier run, before the scan over-credit fix; not repeated.

² Stopped after two ATPG rounds, before compaction.

## How faultflow compares

| | faultflow | Fault (AUCOHL) | Atalanta | Commercial (TetraMAX/TestMAX, Modus, Tessent) |
|---|---|---|---|---|
| License | Apache-2.0 | Apache-2.0 | Academic | Proprietary |
| Input | Yosys gate-level netlist | Gate-level Verilog netlist | ISCAS bench | Industry netlists |
| Test generation | SAT (CaDiCaL) + random | Random/LFSR + Quaigh, Atalanta or PODEM | FAN | Proprietary |
| Stuck-at | Yes | Yes | Yes | Yes |
| Transition (LOC/LOS) | Yes | No | No | Yes; LOC/LOS: Unverified |
| Scan insertion | Yes | Yes | No | Yes |
| IEEE 1500 wrapper | Boundary cells | No | No | Yes |
| Hierarchical SoC roll-up | Yes | No | No | Tessent: Yes; TestMAX, Modus: Unverified |
| Scan compression | No | No | No | Yes |

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

## Part of an open-source DFT toolchain

| Tool | What it does |
|---|---|
| **faultflow** | ATPG and fault simulation for Yosys gate-level netlists |
| [OpenTestability](https://github.com/ranaumarnadeem/OpenTestability) | SCOAP/COP testability analysis and test point insertion; feeds reconvergence data to faultflow |
| [autoMBIST](https://github.com/ranaumarnadeem/autoMBIST) | MBIST, BIRA and BISR generation for OpenRAM memories |
| [warptap](https://github.com/ranaumarnadeem/warptap) | IEEE 1149.1 / 1687 test-access insertion; retargets faultflow scan patterns to JTAG |

## FAQ

**Is there an open-source ATPG tool?**
Yes. faultflow generates stuck-at and transition test patterns for gate-level netlists using a SAT solver, and reports fault coverage.

**Can I run ATPG on a Yosys netlist?**
Yes. faultflow reads technology-mapped Yosys output for sky130 HD and OSU035 cell libraries.

**Does faultflow support scan chains?**
Yes. It inserts full scan, validates chains structurally and runs scan ATPG, including launch-on-capture and launch-on-shift for transition faults.

**What fault coverage does it reach?**
See Results above and [docs/benchmarking.md](docs/benchmarking.md); for example, 99.992% stuck-at test coverage on picorv32a with 2,434 patterns, and 100.00% on the other 32 benchmark designs.

## Citing

Use the "Cite this repository" button on GitHub or [CITATION.cff](CITATION.cff). A paper describing faultflow is under review; this section will link it once published.

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
