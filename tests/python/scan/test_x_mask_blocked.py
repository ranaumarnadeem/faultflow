"""Unknown values the held inputs keep out (faultflow.scan.x_mask.blocked_sources): a
source is blocked when every cell reading it is a gate whose held inputs fix its
output whatever the source is, and the stuck-ats on those holds are the ones that
would let it in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faultflow.scan.atpg_view import TIE0_CELL, TIE1_CELL
from faultflow.scan.x_mask import blocked_sources

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = json.loads(
    (ROOT / "cells/sky130/sky130_fd_sc_hd.json").read_text(encoding="utf-8")
)


def _cell(kind: str, **conns: int) -> dict[str, Any]:
    return {
        "type": f"sky130_fd_sc_hd__{kind}",
        "port_directions": {p: "output" if p == "X" else "input" for p in conns},
        "connections": {p: [n] for p, n in conns.items()},
    }


def _tie(value: int, net: int) -> dict[str, Any]:
    return {
        "type": TIE1_CELL if value else TIE0_CELL,
        "port_directions": {"Y": "output"},
        "connections": {"Y": [net]},
    }


def _view() -> dict[str, Any]:
    """Unknown x1 (net 1) into a mux selected by a tie at 1 (net 9), unknown x2
    (net 2) into an and2 whose other input a tie holds at 0 (net 8), unknown x3
    (net 3) into a mux whose select s (net 7) is a free input, and unknown x4 (net
    4) into an and2 whose other input is unknown x5 (net 5)."""
    cells = {
        "x1": _tie(0, 1),
        "x2": _tie(0, 2),
        "x3": _tie(0, 3),
        "x4": _tie(0, 4),
        "x5": _tie(0, 5),
        "hold1": _tie(1, 9),
        "hold0": _tie(0, 8),
        "m1": _cell("mux2_1", A0=1, A1=6, S=9, X=10),
        "a2": _cell("and2_1", A=2, B=8, X=11),
        "m3": _cell("mux2_1", A0=3, A1=6, S=7, X=12),
        "a4": _cell("and2_1", A=4, B=5, X=13),
    }
    ports = {
        "q": {"direction": "input", "bits": [6]},
        "s": {"direction": "input", "bits": [7]},
        **{f"o{n}": {"direction": "output", "bits": [n]} for n in (10, 11, 12, 13)},
    }
    return {"ports": ports, "cells": cells}


def test_a_source_every_reader_holds_off_is_blocked(require_cpp_core: None) -> None:
    from faultflow.runner.runner import _load_core

    unknown = [1, 2, 3, 4, 5]
    blocked, release = blocked_sources(
        _load_core(), _view(), CELL_MAP, [1, 2, 3, 4], unknown
    )
    assert blocked == {1, 2}
    # The mux's select held at 1 by its tie, the and2's input held at 0: each
    # stuck at the other value, on its stem and on the branch into the gate.
    assert release == {
        ("net:9:stem", "sa0"),
        ("net:9:branch:m1:S", "sa0"),
        ("net:8:stem", "sa1"),
        ("net:8:branch:a2:B", "sa1"),
    }


def test_a_source_some_reader_lets_through_is_not(require_cpp_core: None) -> None:
    from faultflow.runner.runner import _load_core

    view = _view()
    # x1 also reaches an output directly.
    view["cells"]["b1"] = _cell("buf_1", A=1, X=14)
    blocked, release = blocked_sources(_load_core(), view, CELL_MAP, [1], [1])
    assert blocked == frozenset() and release == frozenset()
