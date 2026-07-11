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

```{important}
**Pin to `rel-1.7.4`, don't clone HEAD.** Newer CaDiCaL (3.0.0, confirmed) enables
stricter variable-declaration checking by default that crashes every SAT-based
test and every real ATPG run outright: `cadical: fatal error: invalid API usage
of 'void CaDiCaL::Solver::add(int)'... adding literal '-3' with undeclared
variable '3'`. 1.7.4 is what this repo's CNF encoder (`src/core/atpg/
cnf_encoder.cpp`) is actually compatible with today — it's the version a working
install already runs (Ubuntu's `libcadical-dev` package). Cloning
unpinned HEAD, as an earlier version of this recipe did, will eventually drift
past a compatible version; there's no CI or fingerprint check that would catch
it before you hit the crash above.
```

```bash
git clone --branch rel-1.7.4 https://github.com/arminbiere/cadical.git
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

## Building with Nix

A flake at the repository root (`flake.nix`, plus helper derivations under `nix/`)
packages everything above. Requires Nix with flakes enabled
(`--extra-experimental-features 'nix-command flakes'`, or set
`experimental-features = nix-command flakes` in `nix.conf`).

```{list-table}
:header-rows: 1

* - Output
  - What it gives you
* - `nix develop`
  - The full toolchain: cmake/ninja/gcc, SQLiteCpp, pybind11, CaDiCaL, Yosys,
    Icarus Verilog, Verilator, and a Python environment (jsonschema, rich,
    tkinter, pytest, black, flake8, mypy, the Sphinx doc toolchain). Use this
    for the traditional `cmake -S . -B build -G Ninja && cmake --build build`
    workflow, or to run the test suites (see below).
* - `nix build` / `nix build .#faultflow`
  - The wrapped `faultflow` command: the C++ core, the Python control plane,
    and the compiled `_faultflow_core` extension staged together, with Yosys
    and Icarus Verilog on `PATH`. `result/bin/faultflow`.
* - `nix build .#faultflow-core`
  - Just the C++ side: `libfaultflow_core.a`, the `_faultflow_core*.so`
    extension, and the `faultflow_tests` Catch2 binary.
* - `nix build .#docs`
  - This documentation site as static HTML.
* - `nix run . -- <args>`
  - Run faultflow without building a local `result` symlink, e.g.
    `nix run . -- status --top c17 -c config.ofs`.
```

`nix flake check` (or `nix build .#checks.<system>.faultflow-core` /
`.python-tests` individually) runs **both full test suites fully hermetically** —
the complete Catch2 suite (`ctest`, 201/201) and the complete `tests/python`
suite (`pytest`), inside Nix's sandboxed build, with no manual checkout step and
no `nix develop` required. Both check derivations regenerate the ISCAS benchmark
netlists they need (`tests/benchmarks/**/synth_sky130/*.json`,
`tests/benchmarks/iscas85/synth/c17.json`) via a Yosys `preCheck`/`checkPhase`
step from the tracked RTL sources, using the tracked Liberty corners — see
"What's tracked and why" below.

The wrapped `faultflow` command (`nix build .#faultflow`) bundles the small,
faultflow-authored `cells/**/*.json` cell maps — `[design] cell_lib` works out
of the box, pointing at
`<result>/share/faultflow/cells/sky130/sky130_fd_sc_hd.json` (confirmed by
running the packaged binary from a directory with no faultflow checkout
anywhere nearby). Point `[design] liberty` / `verilog_models` at your own PDK
copy for synthesis or `verify = true` if you need cells beyond what's tracked
(see below).

### What's tracked and why

Nix only sees git-tracked source. A few `.gitignore` rules here were
collaterally over-broad — written to exclude large generated/third-party
content but sweeping up small, necessary files in the same pattern. Fixed by
narrowing the rules rather than by working around them in the Nix packaging:

| Path | Size | Why it's tracked |
|---|---|---|
| `cells/{sky130,osu}/*.json`, `osu035.yml` | ~44KB | faultflow's own gate-semantics maps — was always tracked in git history; `.gitignore` only blocks *new* files, it never retroactively untracks. |
| `cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib` | 13MB | The only Sky130 Liberty corner anything in this repo actually uses (Yosys `dfflibmap`/`abc`). |
| `cells/osu/osu035_stdcells.lib` | 266KB | Same, for the OSU035 path (`tests/python/wrap/test_wbr_atpg.py`). |
| `cells/sky130/sky130_fd_sc_hd.v` | 2.3MB | Behavioral models for iverilog-based verification tests. Explicit `Copyright 2020 The SkyWater PDK Authors`, Apache-2.0. |
| `tests/benchmarks/{iscas85,iscas89}/*.v` | 1.8MB | Public-domain ISCAS85/89 RTL sources (`c17.v` is 529 bytes) — the *inputs* to synthesis. |
| `config.ofs.example` | tiny | Read directly by several `tests/python` cases; was already tracked. |

**Not** tracked, and correctly so: the other two Sky130 Liberty corners (81MB,
genuinely unused anywhere), all other `cells/**/*.v` behavioral models, and
`tests/benchmarks/**/synth_sky130/`, `synth/` — the **generated** Yosys
synthesis *output*, same category as `build/`, regenerated on demand instead of
committed.

A related, real fix landed alongside this: **CaDiCaL is pinned to 1.7.4**
(`nix/cadical.nix`), not nixpkgs' `cadical` (3.0.0 as of writing). The newer
version enables stricter variable-declaration checking that crashes every
SAT-based test and every real ATPG run outright (`cadical: fatal error: invalid
API usage... adding literal '-3' with undeclared variable '3'`) — a real,
previously-latent gap: this repo's own `git clone .../cadical.git` build recipe
below is now pinned to the same tag for the same reason; before this fix it had
no version pin, so a fresh non-Nix install was equally exposed.

Not packaged: **Quaigh** (optional reference/comparison ATPG, `[atpg] tool =
quaigh`) and **`nl2bench`**. `nix/quaigh.nix` builds up to a real, verified
`fetchCrate`/`cargoHash`, but Quaigh's `rustsat-kissat` dependency clones and
compiles [Kissat](https://github.com/arminbiere/kissat) from a `build.rs` at
build time, which cannot work inside Nix's network-disabled sandbox — see the
comment at the top of that file for what a real fix would need. Get Quaigh with
`cargo install quaigh` outside Nix if you need the comparison path; `nl2bench`
has no confirmed upstream source and remains a manually-supplied tool either way.

### Version, overlay, and `nix fmt`

The version is single-sourced from the top-level `VERSION` file: the flake reads
it (`builtins.readFile ./VERSION`) and `faultflow/__init__.py` exposes it as
`faultflow.__version__` (the file is bundled next to the package so it resolves
in an installed layout too). Cutting a release is a one-line bump of `VERSION`
plus a git tag.

`overlays.default` lets a downstream flake pull faultflow into its own nixpkgs
(`overlays = [ faultflow.overlays.default ]` gives `pkgs.faultflow`,
`pkgs.faultflow-core`, `pkgs.faultflow-cadical`). `nix fmt` formats the Nix
tree. `.github/workflows/nix.yml` runs `nix flake check` (both hermetic suites)
on push/PR.

### Publishing to Nix (when the repo is public)

The flake is publish-ready; the only hard prerequisite is that
`github.com/ranaumarnadeem/faultflow` is **public** (it is private today, so the
steps below are recorded for later, not active). Once it is public:

- **Run straight from GitHub** — nothing else needed:
  `nix run github:ranaumarnadeem/faultflow -- status --top c17 -c config.ofs`,
  `nix build github:ranaumarnadeem/faultflow#docs`,
  `nix develop github:ranaumarnadeem/faultflow`.
- **Cut a release (optional):** bump `VERSION`, `git tag v$(cat VERSION)`, push
  the tag. `nix build github:ranaumarnadeem/faultflow/v0.1.0` then pins that tag.
- **Binary cache (optional, recommended):** create a [Cachix](https://cachix.org)
  cache and add a `cachix/cachix-action` push step to `nix.yml`, then publish the
  cache's public key here. Users then get prebuilt binaries instead of compiling
  the C++ core + CaDiCaL on first build.
- **FlakeHub (optional):** add a
  [`DeterminateSystems/flakehub-push`](https://github.com/DeterminateSystems/flakehub-push)
  workflow for a versioned, discoverable listing (works with rolling versions
  even before real tags).
- **nixpkgs upstream (later):** deferred. It needs the CaDiCaL 1.7.4 pin removed
  (real work on `src/core/atpg/cnf_encoder.cpp` to satisfy CaDiCaL 3.x's stricter
  variable-declaration API) and a tagged release; until both land, an upstream PR
  would stall.
