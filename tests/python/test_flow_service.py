from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from faultflow.config import load_config
from faultflow.project.profiles import get_profile, profile_for_cell_map
from faultflow.service import ArtifactPolicy, FlowService


def test_output_root_changes_paths_without_changing_project_inputs(
    tmp_path: Path,
) -> None:
    cfg = load_config(Path("config.ofs.example"), "c17")
    relocated = replace(cfg, output_root=tmp_path / "results")

    assert relocated.output_dir == tmp_path / "results" / "c17"
    assert relocated.db_path == relocated.output_dir / ".faultflow/faultflow.sqlite"
    assert relocated.netlist == cfg.netlist
    assert relocated.cell_lib == cfg.cell_lib


def test_registered_profiles_describe_scan_binding_capability() -> None:
    sky130 = get_profile("sky130")
    osu035 = get_profile("osu035")

    assert sky130.supports_generic_scan is True
    assert sky130.supports_physical_scan is True
    assert osu035.supports_generic_scan is True
    assert osu035.supports_physical_scan is False
    assert profile_for_cell_map(sky130.cell_map).name == "sky130"
    assert profile_for_cell_map(osu035.cell_map).name == "osu035"


def test_workspace_only_scan_policy_uses_hidden_generic_json() -> None:
    cfg = load_config(Path("config.ofs.example"), "c17")
    service = FlowService(artifact_policy=ArtifactPolicy.WORKSPACE_ONLY)

    assert service.scan_generic_json_path(cfg) == (
        cfg.intermediate_dir / "c17_scan.json"
    )


def test_campaign_clean_is_idempotent_and_preserves_manifest(tmp_path: Path) -> None:
    cfg = replace(
        load_config(Path("config.ofs.example"), "c17"),
        output_root=tmp_path,
    )
    cfg.ensure_workspace()
    cfg.scan_manifest_path.write_text("{}\n", encoding="utf-8")
    cfg.db_path.write_text("db", encoding="utf-8")
    Path(f"{cfg.db_path}-wal").write_text("wal", encoding="utf-8")

    first = FlowService().clean_campaigns(cfg)
    second = FlowService().clean_campaigns(cfg)

    assert first.removed == 2
    assert second.removed == 0
    assert cfg.scan_manifest_path.exists()


def test_service_delegates_existing_operations_and_preserves_messages() -> None:
    cfg = load_config(Path("config.ofs.example"), "c17")
    calls: list[tuple[str, object]] = []

    class FakeRunner:
        def __init__(self, _cfg: object) -> None:
            pass

        def init(self) -> str:
            calls.append(("init", None))
            return "initialized output/c17"

        def status(self, scan: bool = False) -> str:
            calls.append(("status", scan))
            return "top=c17 coverage=n/a"

    service = FlowService(runner_factory=FakeRunner)

    assert service.initialize(cfg).message == "initialized output/c17"
    assert service.status(cfg, scan=True).message == "top=c17 coverage=n/a"
    assert calls == [("init", None), ("status", True)]


def test_workspace_scan_delegates_with_hidden_path_and_without_techmap() -> None:
    cfg = load_config(Path("config.ofs.example"), "c17")
    seen: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, _cfg: object) -> None:
            pass

        def scan(self, **kwargs: object) -> str:
            seen.update(kwargs)
            return "scan complete"

    result = FlowService(
        artifact_policy=ArtifactPolicy.WORKSPACE_ONLY,
        runner_factory=FakeRunner,
    ).insert_scan(cfg, scan_chains=2)

    assert result.message == "scan complete"
    assert seen["run_techmap"] is False
    assert seen["generic_json_path"] == cfg.intermediate_dir / "c17_scan.json"
    assert seen["scan_report_path"] == cfg.intermediate_dir / "scan.rpt"
