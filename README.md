# faultflow

Gate-level stuck-at and transition fault simulator with native SAT ATPG, for
post-synthesis netlists from Yosys.

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
