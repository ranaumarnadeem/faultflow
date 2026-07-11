"""Guard 3 must not treat a policy-excluded WBC boundary fault as "unowned".

A block hands off its WBC-outward pin as `wbr_decoupled`, expecting the assembly to
chip-own it. But the SAME canonical fault can be legitimately excluded on the
assembly side for a totally unrelated, chip-wide reason:

  * Policy 3 (clock/reset exclusion) -- a WBC pin that happens to carry a reset
    signal is excluded there by `include_reset_faults=false`, exactly as it would be
    in a flat whole-chip run. It was never going to be tested on EITHER side.
  * Fault collapsing (now on by default) -- the fused EXTEST view's wbc_out cell can
    reduce to a trivial pass-through, so its TO_SYS fault collapses into its driving
    pseudo-input's fault. The equivalence class is still fully represented in the
    assembly's own denominator (via the surviving fault's own row); it's just no
    longer *literally* the WBC pin's own net id.

In both cases the fault is legitimately accounted for -- nobody silently dropped
coverage -- so Guard 3 raising "owned by no other scope" is a false positive; it
conflates "not owned under this exact identity" with "not accounted for anywhere".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.db.sqlite import init_schema
from faultflow.project.aggregate import AggregateError, aggregate_project
from faultflow.project.orchestrator import ScopeRun

_CAMPAIGN_DEFAULTS = dict(
    netlist_hash="h",
    cell_lib_hash="h",
    config_hash="h",
    template_hash="h",
    yosys_version="0",
    faultflow_version="0",
    collapsing=1,
    unsupported_cells="fail",
    include_clock_faults=0,
    include_reset_faults=0,
)


def _netlist(tmp: Path, name: str, top: str, cells: dict) -> Path:
    p = tmp / f"{name}.json"
    p.write_text(
        json.dumps({"modules": {top: {"attributes": {}, "cells": cells}}}),
        encoding="utf-8",
    )
    return p


def _wbc_cell(net: int, *, block: str | None = None, wbc: str = "__wo_x") -> dict:
    attrs: dict = {}
    if block is not None:
        attrs = {"faultflow_block": block, "faultflow_wbc": wbc}
    return {
        "type": "$wbc_out_scan_faultflow",
        "attributes": attrs,
        "connections": {"TO_SYS": [net]},
    }


def _make_db(
    path: Path,
    top: str,
    campaign_type: str,
    faults: list[dict],
) -> None:
    import faultflow.db as dbmod

    with dbmod.connect(path) as conn:
        init_schema(conn)
        cur = conn.execute(
            "INSERT INTO campaigns (campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, collapsing, "
            "unsupported_cells, include_clock_faults, include_reset_faults) VALUES "
            "(:campaign_type, :top, :netlist_hash, :cell_lib_hash, :config_hash, "
            ":template_hash, :yosys_version, :faultflow_version, :collapsing, "
            ":unsupported_cells, :include_clock_faults, :include_reset_faults)",
            {"campaign_type": campaign_type, "top": top, **_CAMPAIGN_DEFAULTS},
        )
        campaign_id = cur.lastrowid
        for f in faults:
            conn.execute(
                "INSERT INTO faults (campaign_id, fault_site_key, net_id, net_name, "
                "compiled_net_index, fault_type, status, exclusion, collapsed_into) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    f["fault_site_key"],
                    f["net_id"],
                    f.get("net_name", f["fault_site_key"]),
                    f["net_id"],
                    f["fault_type"],
                    f["status"],
                    f.get("exclusion", "none"),
                    f.get("collapsed_into"),
                ),
            )
        conn.commit()


def _build_scopes(tmp_path: Path) -> list[ScopeRun]:
    block_json = _netlist(
        tmp_path,
        "block",
        "blkA",
        {
            # A block's own WBC cells carry no faultflow_block/faultflow_wbc tags (that
            # tagging happens only during graybox composition) -- _wbc_pin_index falls
            # back to the cell INSTANCE name as the boundary_wbc identifier, so the
            # instance name here must match the wbc id the assembly tags explicitly.
            "__wo_x": _wbc_cell(5),
            "__wo_y": _wbc_cell(7),
        },
    )
    block_db = tmp_path / "block.sqlite"
    _make_db(
        block_db,
        "blkA",
        "scan",
        [
            # Handed off by the block (wbr_decoupled) -- the assembly must own these.
            {
                "fault_site_key": "net:5:stem",
                "net_id": 5,
                "fault_type": "sa0",
                "status": "excluded",
                "exclusion": "wbr_decoupled",
            },
            {
                "fault_site_key": "net:7:stem",
                "net_id": 7,
                "fault_type": "sa0",
                "status": "excluded",
                "exclusion": "wbr_decoupled",
            },
            # A local, in-denominator fault so the block's chip contribution > 0.
            {
                "fault_site_key": "net:1:stem",
                "net_id": 1,
                "fault_type": "sa0",
                "status": "detected",
                "exclusion": "none",
            },
        ],
    )

    asm_json = _netlist(
        tmp_path,
        "asm",
        "soc_top",
        {
            "u_wo_x": _wbc_cell(9, block="blkA", wbc="__wo_x"),
            "u_wo_y": _wbc_cell(13, block="blkA", wbc="__wo_y"),
        },
    )
    asm_db = tmp_path / "asm.sqlite"
    _make_db(
        asm_db,
        "soc_top",
        "scan",
        [
            # Case 1: excluded by Policy 3 (reset), same as a flat run would.
            {
                "fault_site_key": "net:9:stem",
                "net_id": 9,
                "fault_type": "sa0",
                "status": "excluded",
                "exclusion": "reset",
            },
            # Case 2: collapsed into another (non-WBC) net's surviving fault.
            {
                "fault_site_key": "net:13:stem",
                "net_id": 13,
                "fault_type": "sa0",
                "status": "undetected",
                "exclusion": "none",
                "collapsed_into": 99,
            },
            # The surviving representative of the collapsed fault -- this is what
            # actually contributes to the assembly's (and chip's) denominator.
            {
                "fault_site_key": "net:99:stem",
                "net_id": 99,
                "fault_type": "sa0",
                "status": "detected",
                "exclusion": "none",
            },
        ],
    )

    return [
        ScopeRun("block", "blkA", "blkA", "scan", block_db, block_json, (), ""),
        ScopeRun(
            "interconnect", "soc_top", "soc_top", "scan", asm_db, asm_json, (), ""
        ),
    ]


def test_policy_excluded_handoff_does_not_raise(tmp_path: Path) -> None:
    scopes = _build_scopes(tmp_path)
    chip = aggregate_project("p", scopes)
    assert chip.guards["handoff_complete"] is True
    # The reset-excluded and collapsed WBC faults never enter ANY chip tally --
    # exactly like a flat run applying the same policy/collapsing would.
    assert chip.chip_denominator == 2  # net 1 (block-local) + net 99 (surviving)
    assert chip.chip_detected == 2


def test_truly_unowned_handoff_still_raises(tmp_path: Path) -> None:
    """A handoff with NO corresponding assembly-side row at all (a real gap, not a
    policy exclusion) must still fail loudly."""
    scopes = _build_scopes(tmp_path)
    # Strip the assembly's cells/faults entirely: nothing accounts for blkA's
    # handoff anymore.
    asm_json = scopes[1].netlist
    asm_json.write_text(
        json.dumps({"modules": {"soc_top": {"attributes": {}, "cells": {}}}}),
        encoding="utf-8",
    )
    with pytest.raises(AggregateError, match="owned by no other scope"):
        aggregate_project("p", scopes)
