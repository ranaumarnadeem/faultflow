"""``ff.py cell-map``: write the cell map of a library from its Liberty."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from faultflow.pdk.cell_map_gen import generate_cell_map
from faultflow.pdk.liberty import LibertyError, parse_liberty

# FaultFlow's own cells (the scan flop it stitches, its observe and capture buffers) are
# in every map; they're taken from a shipped map's "$"-named entries.
DEFAULT_INTERNAL_FROM = (
    Path(__file__).resolve().parents[2] / "cells/sky130/sky130_fd_sc_hd.json"
)


def internal_cells(source: Path) -> dict[str, Any]:
    """The entries of the cell map at ``source`` that name FaultFlow's own cells."""
    cm = json.loads(Path(source).read_text(encoding="utf-8"))
    return {k: v for k, v in cm.items() if k.lstrip("\\").startswith("$")}


def cell_map_command(
    liberty: Path,
    out: Path,
    *,
    report: Path | None = None,
    internal_from: Path = DEFAULT_INTERNAL_FROM,
) -> str:
    from faultflow.runner.runner import _load_core

    core = _load_core()
    if core is None:
        raise LibertyError("ff.py cell-map needs the C++ core (its gate evaluation)")
    try:
        text = Path(liberty).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LibertyError(f"cannot read {liberty}: {exc}") from exc
    library = parse_liberty(text)
    result = generate_cell_map(
        library, core, internal_cells=internal_cells(internal_from)
    )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.cell_map, indent=2) + "\n", encoding="utf-8")
    if report is not None:
        Path(report).write_text(
            json.dumps([asdict(r) for r in result.report], indent=2) + "\n",
            encoding="utf-8",
        )
    cells = len(library.all("cell"))
    listed = len({r.cell for r in result.report if r.kind != "tristate"})
    tristate = len({r.cell for r in result.report if r.kind == "tristate"})
    modelled = cells - listed - tristate
    lines = [
        f"cell-map {library.args[0] if library.args else liberty}: {cells} cells, "
        f"{modelled} modelled, {listed} listed, {tristate} tristate -> {out}"
    ]
    for r in result.report:
        if r.kind not in ("tristate",):
            lines.append(f"  {r.cell}: {r.detail}")
    return "\n".join(lines)
