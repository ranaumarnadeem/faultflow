# faultflow

Gate-level stuck-at fault simulator for post-synthesis netlists from Yosys.

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

To reset only the SQLite database (not the whole output tree):

```bash
python3 ff.py sim --top <top> --clean
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
