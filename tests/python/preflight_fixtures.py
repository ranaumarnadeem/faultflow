"""A reconvergent netlist and a fake OpenTestability `_preflight` for it.

With `opentest` on PATH, ATPG runs `opentest _preflight --reconv-algorithm
advanced`, whose records name every stem of a reconvergent fanout pair (Xu &
Edirisuriya's FOBL/RFOBL detector). The netlist has two such stems, and real
OT reports exactly those two:

    Y = XOR(BUF(A), BUF(A))    A's stem faults are redundant (both branches
                               flip together), but each A branch fault is not
    Z = (S & P) | (~S & R)     S reconverges at the OR, yet every S fault,
                               the stem's included, is testable

OT_FANOUT_POINTS / OT_RECONVERGENCES are what real OT wrote for it (tpi
manifest `fanout_points`, `<design>_reconv_ids.json` records).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultflow.db import connect, init_schema

TOP = "mini_reconv"
STEM_A = 5
STEM_S = 9
FAKE_OPENTEST = "/fake/bin/opentest"


def _pair(branch1: str, branch2: str, stem: str, stem_id: int) -> dict[str, Any]:
    return {
        "branch1": branch1,
        "branch2": branch2,
        "path1_count": 1,
        "path2_count": 1,
        "stem": stem,
        "branch1_net_id": stem_id if branch1 == stem else None,
        "branch2_net_id": None,
        "stem_net_id": stem_id,
    }


OT_FANOUT_POINTS = [
    {"name": "S", "yosys_net_id": STEM_S},
    {"name": "A", "yosys_net_id": STEM_A},
]
OT_RECONVERGENCES = [
    {
        "site": "Y",
        "site_net_id": 8,
        "pairs": [
            _pair("A", "A_br1", "A", STEM_A),
            _pair("A", "A_br0", "A", STEM_A),
            _pair("A_br0", "A_br1", "A", STEM_A),
        ],
    },
    {"site": "Z", "site_net_id": 15, "pairs": [_pair("S", "S_br0", "S", STEM_S)]},
]


def _cell(kind: str, conns: dict[str, list[int]]) -> dict[str, Any]:
    return {
        "hide_name": 0,
        "type": f"sky130_fd_sc_hd__{kind}",
        "parameters": {},
        "attributes": {},
        "port_directions": {
            pin: "output" if pin in ("Q", "X", "Y") else "input" for pin in conns
        },
        "connections": conns,
    }


def write_reconvergent_netlist(path: Path, *, with_flop: bool) -> Path:
    """`with_flop` adds a lone D flop (CLK, D -> Q) for a scan chain to hold;
    it touches none of the logic above."""
    ports = {
        "A": ("input", STEM_A),
        "Y": ("output", 8),
        "S": ("input", STEM_S),
        "P": ("input", 10),
        "R": ("input", 11),
        "Z": ("output", 15),
    }
    cells = {
        "g_b1": _cell("buf_1", {"A": [STEM_A], "X": [6]}),
        "g_b2": _cell("buf_1", {"A": [STEM_A], "X": [7]}),
        "g_x": _cell("xor2_1", {"A": [6], "B": [7], "X": [8]}),
        "g_a1": _cell("and2_1", {"A": [STEM_S], "B": [10], "X": [12]}),
        "g_i": _cell("inv_1", {"A": [STEM_S], "Y": [13]}),
        "g_a2": _cell("and2_1", {"A": [13], "B": [11], "X": [14]}),
        "g_o": _cell("or2_1", {"A": [12], "B": [14], "X": [15]}),
    }
    if with_flop:
        ports.update(CLK=("input", 2), D=("input", 3), Q=("output", 4))
        cells["u0"] = _cell("dfxtp_1", {"CLK": [2], "D": [3], "Q": [4]})
    bits = {name: bit for name, (_direction, bit) in ports.items()}
    bits.update(a_b1=6, a_b2=7, s_and=12, s_n=13, sn_and=14)
    module = {
        "attributes": {"top": "1"},
        "ports": {
            name: {"direction": direction, "bits": [bit]}
            for name, (direction, bit) in ports.items()
        },
        "cells": cells,
        "netnames": {
            name: {"hide_name": 0, "bits": [bit], "attributes": {}}
            for name, bit in bits.items()
        },
    }
    path.write_text(
        json.dumps({"modules": {TOP: module}}, indent=2) + "\n", encoding="utf-8"
    )
    return path


def install_fake_opentest(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Put an `opentest` on PATH whose `_preflight` writes real OT's output for
    the netlist above. Returns the argv of every call it answers."""
    calls: list[list[str]] = []
    real_which = shutil.which
    real_run = subprocess.run

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        if name == "opentest":
            return FAKE_OPENTEST
        return real_which(name, *args, **kwargs)

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(cmd, list) or not cmd or cmd[0] != FAKE_OPENTEST:
            return real_run(cmd, *args, **kwargs)
        calls.append(list(cmd))
        out = Path(cmd[cmd.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        manifest = out / "tpi_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "structural_hints": {
                        "enabled": True,
                        "algorithm": "advanced",
                        "fanout_points": OT_FANOUT_POINTS,
                        "reconvergences": OT_RECONVERGENCES,
                    },
                }
            ),
            encoding="utf-8",
        )
        design = Path(cmd[cmd.index("-i") + 1]).stem
        reconv = out / f"{design}_reconv_ids.json"
        reconv.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "algorithm": "advanced",
                    "reconvergences": OT_RECONVERGENCES,
                }
            ),
            encoding="utf-8",
        )
        summary = {
            "status": "ok",
            "manifest": str(manifest),
            "reconv_ids": str(reconv),
            "tech": "sky130",
        }
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"progress\n{json.dumps(summary)}\n", stderr=""
        )

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def fault_statuses(db_path: Path, campaign_id: int) -> dict[tuple[str, str], str]:
    """(site key, fault type) -> status for every fault inside the denominator."""
    with connect(db_path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT fault_site_key, fault_type, status
            FROM faults
            WHERE campaign_id = ? AND exclusion = 'none'
            """,
            (campaign_id,),
        ).fetchall()
    return {
        (str(r["fault_site_key"]), str(r["fault_type"])): str(r["status"]) for r in rows
    }


def assert_only_proven_faults_redundant(
    hinted: dict[tuple[str, str], str], reference: dict[tuple[str, str], str]
) -> None:
    """`hinted` ran with OT's preflight, `reference` without it. (A helper
    module's asserts are not rewritten by pytest: each carries its data.)"""
    a_branches = {
        site: hinted[site]
        for consumer in ("g_b1", "g_b2")
        for fault_type in ("sa0", "sa1")
        for site in [(f"net:{STEM_A}:branch:{consumer}:A", fault_type)]
    }
    # A branch reaches one reader: testable although its stem is not.
    assert set(a_branches.values()) == {"detected"}, a_branches
    a_stem = {site: s for site, s in hinted.items() if site[0] == f"net:{STEM_A}:stem"}
    assert set(a_stem.values()) == {"redundant"} and len(a_stem) == 2, a_stem
    # S reconverges as well, and every S fault is testable, stem included.
    s_sites = {
        site: status
        for site, status in hinted.items()
        if site[0].startswith(f"net:{STEM_S}:")
    }
    assert set(s_sites.values()) == {"detected"} and len(s_sites) == 6, s_sites
    # The hint may order work; it must not change a single verdict.
    changed = {
        site: (status, reference.get(site))
        for site, status in hinted.items()
        if reference.get(site) != status
    }
    assert not changed and hinted.keys() == reference.keys(), changed
