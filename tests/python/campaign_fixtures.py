from __future__ import annotations

import pytest
from pathlib import Path

from faultflow.config import FaultflowConfig
from faultflow.db import CAMPAIGN_TYPE_COMB, connect, ensure_campaign, init_schema
from faultflow.runner.runner import Runner


def campaign_id_for_cfg(
    cfg: FaultflowConfig, netlist: Path, *, scan: bool = False
) -> int:
    runner = Runner(cfg)
    fp = runner._fingerprint(netlist)
    if scan:
        fp.setdefault("manifest_hash", "")
        fp.setdefault("atpg_view_schema_ver", "")
    with connect(cfg.db_path) as conn:
        init_schema(conn)
        return ensure_campaign(
            conn,
            "scan" if scan else CAMPAIGN_TYPE_COMB,
            fp,
        )


def require_campaign_id(
    cfg: FaultflowConfig, netlist: Path, monkeypatch: pytest.MonkeyPatch | None = None
) -> int:
    if monkeypatch is not None:
        monkeypatch.chdir(cfg.path.parent)
    return campaign_id_for_cfg(cfg, netlist)
