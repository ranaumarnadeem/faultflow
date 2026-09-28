"""Tests for `faultflow.integrations.autombist_coverage`: every fault goes to
the instance of the cell it sits on, and the categories add up."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from faultflow.integrations.autombist_coverage import GLUE, category_coverage

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "top"
CATEGORIES = {
    "u_algo": "mbist_controller",
    "u_mem": "memory",
    "warptap_sib_go": "ijtag_sib",
    "warptap_sib_go_inst_0": "ijtag_tdr",
}


def _view(tmp_path: Path) -> Path:
    """A scan ATPG view in miniature. Spliced library cells carry no port
    directions (Yosys knows no sky130 types), so their drivers come from the
    cell map."""

    def lib(cell_type: str, **pins: int) -> dict[str, Any]:
        return {"type": cell_type, "connections": {p: [b] for p, b in pins.items()}}

    cells = {
        # A Yosys cell name may hold colons.
        "u_algo__$abc$1$auto$blifparse.cc:397:parse_blif$2": lib(
            "sky130_fd_sc_hd__nand2_1", A=2, B=10, Y=11
        ),
        "warptap_sib_go_inst_0__g": lib("sky130_fd_sc_hd__inv_1", A=11, Y=12),
        "warptap_sib_go__g": lib("sky130_fd_sc_hd__inv_1", A=12, Y=13),
        "g_glue": lib("sky130_fd_sc_hd__buf_1", A=2, X=14),
        # The scan flow's helper for flop u_algo__state_reg.
        "$ffbranch_u_algo__state_reg": {
            "type": "$faultflow_d_branch_buf",
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [11], "Y": [15]},
        },
        # The memory's output tie and input reader.
        "$bbtie0_u_mem_dout_0": {
            "type": "sky130_fd_sc_hd__conb_1",
            "attributes": {"faultflow_blackbox": "u_mem"},
            "port_directions": {"Y": "output"},
            "connections": {"Y": [16]},
        },
        "$bbsink_u_mem_din_0": {
            "type": "$faultflow_observe_buf",
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [14], "Y": [17]},
        },
    }
    ports = {
        "pi": {"direction": "input", "bits": [2]},
        "__ppi_u_algo__state_reg": {"direction": "input", "bits": [10]},
        "__ppo_u_algo__state_reg": {"direction": "output", "bits": [15]},
    }
    path = tmp_path / "view.json"
    path.write_text(
        json.dumps(
            {
                "modules": {
                    TOP: {"attributes": {"top": "1"}, "ports": ports, "cells": cells}
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _db(faults: list[tuple[str, int, str, int]]) -> sqlite3.Connection:
    """(site key, net id, status, blackbox_unresolved) rows of campaign 1."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE faults (campaign_id INTEGER, fault_site_key TEXT, net_id INTEGER,"
        " status TEXT, exclusion TEXT, collapsed_into INTEGER,"
        " blackbox_unresolved INTEGER)"
    )
    conn.executemany(
        "INSERT INTO faults VALUES (1, ?, ?, ?, 'none', NULL, ?)",
        faults,
    )
    return conn


@pytest.mark.unit
@pytest.mark.parametrize(
    ("site_key", "net_id", "category"),
    [
        # A branch is its consumer's.
        (
            "net:11:branch:warptap_sib_go_inst_0__g:A",
            11,
            "ijtag_tdr",
        ),
        (
            "net:2:branch:u_algo__$abc$1$auto$blifparse.cc:397:parse_blif$2:A",
            2,
            "mbist_controller",
        ),
        # A stem is its driver's, found through the cell map.
        ("net:11:stem", 11, "mbist_controller"),
        ("net:13:stem", 13, "ijtag_sib"),
        # A flop's pseudo-input stands for the flop.
        ("net:10:stem", 10, "mbist_controller"),
        # The scan flow's helper cells are the flop's.
        ("net:15:stem", 15, "mbist_controller"),
        ("net:11:branch:$ffbranch_u_algo__state_reg:A", 11, "mbist_controller"),
        # The memory's boundary: its output tie and its input reader.
        ("net:16:stem", 16, "memory"),
        ("net:14:branch:$bbsink_u_mem_din_0:A", 14, "memory"),
        # The wrapper's own logic, a primary input, a constant.
        ("net:14:stem", 14, GLUE),
        ("net:2:stem", 2, GLUE),
        ("net:-1:stem", -1, GLUE),
    ],
)
def test_a_fault_is_counted_for_the_instance_it_sits_in(
    tmp_path: Path, site_key: str, net_id: int, category: str
) -> None:
    counts = category_coverage(
        _db([(site_key, net_id, "detected", 0)]),
        1,
        netlist_json=_view(tmp_path),
        cell_map_json=CELL_MAP,
        top=TOP,
        categories=CATEGORIES,
    )
    assert counts == {
        category: {
            "detected": 1,
            "denominator": 1,
            "blackbox_unresolved": 0,
            "coverage_percent": 100.0,
        }
    }


@pytest.mark.unit
def test_categories_count_the_way_the_summary_does(tmp_path: Path) -> None:
    conn = _db(
        [
            ("net:11:stem", 11, "detected", 0),
            ("net:11:stem", 11, "undetected", 1),
            ("net:13:stem", 13, "redundant", 0),
            ("net:13:stem", 13, "undetected", 0),
        ]
    )
    # An excluded fault and a collapsed one.
    conn.executemany(
        "INSERT INTO faults VALUES (1, 'net:11:stem', 11, 'undetected', ?, ?, 0)",
        [("clock", None), ("none", 7)],
    )

    counts = category_coverage(
        conn,
        1,
        netlist_json=_view(tmp_path),
        cell_map_json=CELL_MAP,
        top=TOP,
        categories=CATEGORIES,
    )

    # Excluded and collapsed faults count nowhere; a redundant one is out of
    # the denominator.
    assert counts == {
        "ijtag_sib": {
            "detected": 0,
            "denominator": 1,
            "blackbox_unresolved": 0,
            "coverage_percent": 0.0,
        },
        "mbist_controller": {
            "detected": 1,
            "denominator": 2,
            "blackbox_unresolved": 1,
            "coverage_percent": 50.0,
        },
    }


@pytest.mark.unit
def test_a_missing_manifest_is_a_config_error(tmp_path: Path) -> None:
    from faultflow.config import ConfigError, load_config

    ofs = tmp_path / "design.ofs"
    ofs.write_text(
        "[design]\nnetlist = design.json\n\n"
        f"[autombist]\nmanifest = {tmp_path / 'moved' / 'manifest.json'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"\[autombist\] manifest not found"):
        load_config(ofs, TOP)
