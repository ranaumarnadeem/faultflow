# Installation

```{important}
faultflow supports **Unix-like systems only**: Linux, or Windows through
**WSL2**. All build artifacts, scripts, and the Python entry point assume a POSIX
environment (bash, symlinks, an ELF Python extension). There is **no native Windows
(cmd/PowerShell) build**. On Windows, do everything inside a WSL2 shell.
```

faultflow has two halves that are built separately:

- a **C++17 core** compiled with CMake into a static library, a Catch2 test binary,
  and a `_faultflow_core` Python extension module; and
- a **Python control plane** run directly from the source tree (there is no
  `pip install` step yet).

## Prerequisites

### Build toolchain

| Requirement | Notes |
|---|---|
| C++17 compiler | GCC or Clang (the project builds with `-Wall -Wextra`) |
| CMake | 3.20 or newer |
| Ninja or GNU Make | either generator works |
| Python | 3.10+ (the reference environment uses 3.12) |

### C++ libraries

| Library | How it is obtained |
|---|---|
| [Catch2](https://github.com/catchorg/Catch2) 3.x | fetched automatically by CMake (`FetchContent`) |
| [nlohmann/json](https://github.com/nlohmann/json) 3.11+ | fetched automatically by CMake (`FetchContent`) |
| [SQLiteCpp](https://github.com/SRombauts/SQLiteCpp) | found on the system (`find_package(SQLiteCpp)`) |
| [pybind11](https://pybind11.readthedocs.io/) 2.11+ | found on the system (`find_package(pybind11 CONFIG)`) |
| [CaDiCaL](https://github.com/arminbiere/cadical) | linked as `-lcadical` — **must be installed on the system** |

```{warning}
**CaDiCaL must be installed before you build.** The core links the SAT solver, so
`libcadical` and its header (`cadical.hpp`) must be present. CMake locates them with
`find_library`/`find_path` and fails early with a clear message if they are missing.
Install CaDiCaL (recipe below) and rebuild.
```

### External command-line tools

These are invoked as subprocesses and resolved on your `PATH`. Install the ones you
need for your workflow:

| Tool | Needed for | Required? |
|---|---|---|
| [Yosys](https://yosyshq.net/yosys/) 0.61+ | synthesizing Verilog inputs; scan-cell techmap | required when your input is Verilog (not pre-synthesized JSON) |
| [Icarus Verilog](https://steveicarus.github.io/iverilog/) (`iverilog`/`vvp`) | the optional verification gate | only when `verify = true` |
| `nl2bench` | converting gate-level Verilog to BENCH for Quaigh | only on the optional Quaigh path |
| [Quaigh](https://github.com/coloquinte/quaigh) | reference/comparison ATPG | only when `[atpg] tool = quaigh` |

See [External tools](../external_tools.md) for how each one is wired in.

On Debian/Ubuntu (including Ubuntu under WSL2), the system packages are roughly:

```bash
sudo apt update
sudo apt install -y build-essential cmake ninja-build git python3 python3-venv \
                    pybind11-dev libsqlitecpp-dev yosys iverilog
```

CaDiCaL is not reliably packaged with its development headers, so the most portable path
is to build and install it from source. This puts `libcadical` and `cadical.hpp` on the
default search paths, so the core's bare `-lcadical` link resolves:

```bash
git clone https://github.com/arminbiere/cadical.git
cd cadical
./configure && make
sudo install -m 0644 src/cadical.hpp   /usr/local/include/
sudo install -m 0644 build/libcadical.a /usr/local/lib/
cd ..
```

## Build the C++ core

From the repository root:

```bash
cmake -S . -B build -G Ninja
cmake --build build -- -j2
```

This produces, among other artifacts, the Python extension module at:

```text
build/src/core/_faultflow_core.cpython-3xx-x86_64-linux-gnu.so
```

There is **no install/copy step** for the extension. The Python layer adds
`build/src/core` to `sys.path` at import time, so as long as you built into `build/`
the CLI will find it automatically.

## Set up the Python environment

The control plane runs from the source tree (there is no `pip install` step for
faultflow itself yet). Create a virtual environment and install the dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt        # runtime (jsonschema)
pip install -r requirements-dev.txt    # adds pytest, black, flake8, mypy
```

```{note}
The control plane is otherwise Python standard library only. `requirements.txt` pins the
one runtime dependency (`jsonschema`, used to validate the JSON reports); a built-in
shape check runs if it is absent.
```

A convenience wrapper, `bin/faultflow`, execs `venv/bin/python ff.py "$@"`. If you
symlink it onto your `PATH` (or activate the venv), you can call `faultflow` instead
of `python3 ff.py`.

## Verify the installation

Run the C++ unit tests and (optionally) the Python tests:

```bash
ctest --test-dir build --output-on-failure
PYTHONPATH=. venv/bin/pytest tests/python -q
```

Then run the bundled `c17` benchmark end to end:

```bash
cp config.ofs.example config.ofs
python3 ff.py init --top c17 -c config.ofs
python3 ff.py sim  --top c17 -c config.ofs
python3 ff.py status --top c17 -c config.ofs
```

If `status` prints a coverage summary, the core extension, the Python layer, and the
SQLite campaign database are all working. Continue with the
[Quick start](quickstart.md).

## A note on Nix

faultflow is intended to be packaged with [Nix](https://nixos.org/) in the future.
The documentation toolchain (Sphinx, Furo, MyST) was chosen for clean nixpkgs
mapping; the doc dependencies are pinned in [`docs/requirements.txt`](https://github.com/ranaumarnadeem/faultflow/blob/main/docs/requirements.txt)
so they can be translated to a Nix derivation. A Nix flake for the tool itself is not
yet provided.
