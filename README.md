# faultflow

Gate-level stuck-at and transition fault simulator with native SAT ATPG, for
post-synthesis netlists from Yosys. Driven from an interactive Tcl shell
(`python3 ff.py shell`) or a batch CLI (`python3 ff.py <command>`) for scripted,
one-shot runs.

Full documentation: https://ranaumarnadeem.github.io/faultflow/

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
