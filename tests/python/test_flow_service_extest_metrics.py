"""`FlowService.run_atpg` must report metrics under the campaign the run actually
wrote, not just "scan" vs "comb".

Scan-model EXTEST (`cfg.test_mode == "extest"` + `scan=True`) routes to `_sim_extest`
(see `runner.py`), which records its campaign as `CAMPAIGN_TYPE_SCAN_EXTEST`
("scan_extest") specifically so it never collides with the block's own INTEST/scan
campaign in the same DB. But `run_atpg` picked metrics via a blunt
`"scan" if scan else "comb"`, so a scan-model EXTEST run always looked up the WRONG
campaign type and reported `campaign_state: "not_run"` even though the run succeeded
and a real "scan_extest" campaign exists.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from faultflow.config import load_config
from faultflow.db.sqlite import init_schema
from faultflow.service import FlowService

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


def _seed_scan_extest_campaign(db_path: Path, top: str) -> None:
    import faultflow.db as dbmod

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with dbmod.connect(db_path) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO campaigns (campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, collapsing, "
            "unsupported_cells, include_clock_faults, include_reset_faults) VALUES "
            "(:campaign_type, :top, :netlist_hash, :cell_lib_hash, :config_hash, "
            ":template_hash, :yosys_version, :faultflow_version, :collapsing, "
            ":unsupported_cells, :include_clock_faults, :include_reset_faults)",
            {"campaign_type": "scan_extest", "top": top, **_CAMPAIGN_DEFAULTS},
        )
        conn.commit()


class _StubRunner:
    def __init__(self, cfg: object) -> None:
        self.cfg = cfg

    def sim(self, **options: object) -> str:
        return "sim complete top=soc_top mode=extest coverage=86.111%"


def test_run_atpg_reports_scan_extest_metrics_for_extest_scan_runs(
    tmp_path: Path,
) -> None:
    cfg = replace(
        load_config(Path("config.ofs.example"), "c17"),
        top="soc_top",
        test_mode="extest",
        output_root=tmp_path,
    )
    _seed_scan_extest_campaign(cfg.db_path, "soc_top")

    service = FlowService(runner_factory=_StubRunner)

    result = service.run_atpg(cfg, scan=True)

    assert result.metrics["campaign_state"] == "available"
    assert result.metrics["campaign_type"] == "scan_extest"


def test_run_atpg_still_reports_plain_scan_metrics_for_intest(tmp_path: Path) -> None:
    """Back-compat: block INTEST (test_mode != "extest") keeps using "scan"."""
    cfg = replace(
        load_config(Path("config.ofs.example"), "c17"),
        top="blkA",
        test_mode="intest",
        output_root=tmp_path,
    )

    import faultflow.db as dbmod

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    with dbmod.connect(cfg.db_path) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO campaigns (campaign_type, top, netlist_hash, cell_lib_hash, "
            "config_hash, template_hash, yosys_version, faultflow_version, collapsing, "
            "unsupported_cells, include_clock_faults, include_reset_faults) VALUES "
            "(:campaign_type, :top, :netlist_hash, :cell_lib_hash, :config_hash, "
            ":template_hash, :yosys_version, :faultflow_version, :collapsing, "
            ":unsupported_cells, :include_clock_faults, :include_reset_faults)",
            {"campaign_type": "scan", "top": "blkA", **_CAMPAIGN_DEFAULTS},
        )
        conn.commit()

    service = FlowService(runner_factory=_StubRunner)

    result = service.run_atpg(cfg, scan=True)

    assert result.metrics["campaign_state"] == "available"
    assert result.metrics["campaign_type"] == "scan"
