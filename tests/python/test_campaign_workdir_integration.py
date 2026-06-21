from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.coverage.site_key import SiteProvenance, canonical_site_key

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/cpp/fixtures/tiny_reconverge.json"
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"


@pytest.mark.golden
def test_reconverge_site_keys_match_cpp_export(require_cpp_core: None) -> None:
    import _faultflow_core as core  # type: ignore[import-not-found]

    rows = core.list_site_keys(str(FIXTURE), str(CELL_MAP), "fail")
    keys = {row["site_key"] for row in rows}
    assert "net:2:stem" in keys
    assert "net:2:branch:u0:A" in keys
    assert "net:2:branch:u1:A" in keys
    assert len(keys) == len(rows)

    for row in rows:
        if row["kind"] == "branch":
            prov = SiteProvenance(
                yosys_net_id=int(row["yosys_net_id"]),
                kind="branch",
                consumer_instance=str(row["consumer_instance"]),
                input_pin=str(row["input_pin"]),
            )
        else:
            prov = SiteProvenance(
                yosys_net_id=int(row["yosys_net_id"]),
                kind="stem",
            )
        assert canonical_site_key(prov) == row["site_key"]
