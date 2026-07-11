# faultflow

**faultflow** is a gate-level fault simulator and native SAT ATPG engine for
post-synthesis netlists produced by [Yosys](https://yosyshq.net/yosys/). It grades
stuck-at and transition faults, generates test patterns with a built-in SAT-based
ATPG, and reports fault coverage against a precisely defined denominator.

It is built as a Python control plane over a C++17 simulation core, with the two
joined by a [pybind11](https://pybind11.readthedocs.io/) bridge.

```{note}
faultflow targets **Unix-like systems only** (Linux, or Windows via WSL). There is
no native Windows build. See [Installation](getting_started/installation.md).
```

## What it does

- **Stuck-at and transition fault grading** on flat, post-synthesis gate-level
  netlists, using a 64-lane bit-parallel simulation engine cross-checked against a
  scalar golden reference.
- **Native SAT ATPG** (CaDiCaL) that produces simulator-verified test vectors, with
  a progressive loop, fault collapsing, and reverse-order test-set compaction.
- **Sequential and scan support**: edge-triggered flip-flops, asynchronous
  set/reset, scan-chain insertion, and scan ATPG.
- **Two PDKs**: SkyWater Sky130 HD (default) and OSU035, driven by JSON cell maps.
- **Deterministic coverage reporting** with an explicit, auditable denominator.

## Getting started

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Installation
:link: getting_started/installation
:link-type: doc
Build the C++ core and set up the Python environment on Linux or WSL.
:::

:::{grid-item-card} Quick start
:link: getting_started/quickstart
:link-type: doc
Run your first fault-simulation campaign on the `c17` benchmark in three commands.
:::

:::{grid-item-card} Concepts
:link: getting_started/concepts
:link-type: doc
Stuck-at faults, ATPG, the coverage denominator, and scan in plain terms.
:::

:::{grid-item-card} CLI reference
:link: user_guide/cli_reference
:link-type: doc
Every command and flag for `ff.py` / `faultflow`.
:::

::::

## Learn more

- [About faultflow](about.md) — the feature matrix and a high-level tour.
- [Architecture overview](architecture/overview.md) — the three-layer IR and the
  simulation engine.
- [External tools](external_tools.md) — how faultflow integrates with Yosys,
  OpenTestability, iverilog, and others.
- [Roadmap](roadmap.md) — what is planned next.

```{toctree}
:hidden:
:caption: Introduction

about
```

```{toctree}
:hidden:
:caption: Getting Started

getting_started/installation
getting_started/quickstart
getting_started/concepts
```

```{toctree}
:hidden:
:caption: User Guide

user_guide/cli_reference
user_guide/configuration
user_guide/tcl_shell
user_guide/examples
user_guide/outputs
```

```{toctree}
:hidden:
:caption: Architecture

architecture/overview
architecture/cell_libraries
collapsing_rules
```

```{toctree}
:hidden:
:caption: Reference

benchmarking
external_tools
contributing
roadmap
```
