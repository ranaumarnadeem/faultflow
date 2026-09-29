"""The opt-in non-scan policy: flops matching ``[scan] nonscan_cells`` stay out of the
scan chains (a JTAG TAP and its IJTAG network, tested through TCK instead), and
``[scan] hold`` inputs are held at a constant in every scan pattern."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.rule_check.model import Severity
from faultflow.rule_check.rules import rules_scan
from faultflow.runner import Runner, RunnerError
from faultflow.scan import stitch_scan_json
from faultflow.scan.reports import hash_file, manifest_from_result, utc_timestamp
from faultflow.scan.stitch import IneligibleFF, scan_clock_domains

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "mini"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"
DFRTP = "sky130_fd_sc_hd__dfrtp_1"


def _flop(cell_type: str, **pins: int) -> dict[str, object]:
    return {
        "hide_name": 0,
        "type": cell_type,
        "parameters": {},
        "attributes": {},
        "port_directions": {p: "output" if p == "Q" else "input" for p in pins},
        "connections": {p: [net] for p, net in pins.items()},
    }


def _mini_json() -> dict[str, object]:
    """u0: a functional flop on clk. u_tap__st: a TAP flop on tck, cleared by trst_n."""
    ports = {"clk": 2, "d": 3, "q": 4, "tck": 5, "trst_n": 6, "tdi": 7, "tdo": 8}
    outputs = {"q", "tdo"}
    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": {
                    name: {
                        "direction": "output" if name in outputs else "input",
                        "bits": [net],
                    }
                    for name, net in ports.items()
                },
                "cells": {
                    "u0": _flop(DFXTP, CLK=2, D=3, Q=4),
                    "u_tap__st": _flop(DFRTP, CLK=5, D=7, RESET_B=6, Q=8),
                },
                "netnames": {
                    name: {"hide_name": 0, "bits": [net], "attributes": {}}
                    for name, net in ports.items()
                },
            }
        }
    }


def _write_ofs(path: Path, netlist: Path, scan: str = "") -> Path:
    path.write_text(
        f"""[design]
netlist = {netlist}
cell_lib = {CELL_MAP}

[scan]
chains = 1
{scan}
""",
        encoding="utf-8",
    )
    return path


def _netlist(tmp_path: Path) -> Path:
    source = tmp_path / "mini.json"
    source.write_text(json.dumps(_mini_json()), encoding="utf-8")
    return source


@pytest.mark.unit
def test_scan_config_reads_nonscan_cells_and_holds(tmp_path: Path) -> None:
    ofs = _write_ofs(
        tmp_path / "mini.ofs",
        _netlist(tmp_path),
        "nonscan_cells = u_tap__*, u_sib_*\nhold = trst_n:0, tck:0",
    )
    cfg = load_config(ofs, TOP)
    assert cfg.scan.nonscan_cells == ("u_tap__*", "u_sib_*")
    assert cfg.scan.hold == (("trst_n", 0), ("tck", 0))
    plain = load_config(_write_ofs(tmp_path / "plain.ofs", _netlist(tmp_path)), TOP)
    assert (plain.scan.nonscan_cells, plain.scan.hold) == ((), ())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hold", "message"),
    [
        ("trst_n", "must be '<input>:<0|1>'"),
        ("trst_n:2", "must be '<input>:<0|1>'"),
        ("trst_n:0, trst_n:1", "names an input twice"),
    ],
)
def test_scan_hold_entries_are_checked(tmp_path: Path, hold: str, message: str) -> None:
    ofs = _write_ofs(tmp_path / "mini.ofs", _netlist(tmp_path), f"hold = {hold}")
    with pytest.raises(ConfigError, match=rf"\[scan\] hold .*{message}"):
        load_config(ofs, TOP)


@pytest.mark.unit
def test_a_flop_matching_a_nonscan_glob_stays_out_of_scan(tmp_path: Path) -> None:
    source = _netlist(tmp_path)
    result = stitch_scan_json(
        source, CELL_MAP, TOP, tmp_path / "scan.json", nonscan_cells=("u_tap__*",)
    )

    assert result.ineligible_ffs == [IneligibleFF("u_tap__st", DFRTP, "nonscan_policy")]
    assert [cell.instance for cell in result.cells] == ["u0"]
    stitched = json.loads((tmp_path / "scan.json").read_text(encoding="utf-8"))
    assert stitched["modules"][TOP]["cells"]["u_tap__st"]["type"] == DFRTP
    # Its clock is no scan clock domain: one chain on clk only.
    assert scan_clock_domains(source, CELL_MAP, TOP) == {2: 1, 5: 1}
    assert scan_clock_domains(source, CELL_MAP, TOP, ("u_tap__*",)) == {2: 1}


@pytest.mark.unit
def test_rule_check_reports_a_nonscan_flop_as_info(tmp_path: Path) -> None:
    source = _netlist(tmp_path)
    result = stitch_scan_json(
        source, CELL_MAP, TOP, tmp_path / "scan.json", nonscan_cells=("u_tap__*",)
    )
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)

    violations = rules_scan(manifest)

    assert [(v.rule_id, v.severity) for v in violations] == [("SCAN011", Severity.INFO)]
    assert "u_tap__st" in violations[0].message


@pytest.mark.unit
def test_nonscan_config_is_fingerprinted_only_when_set(tmp_path: Path) -> None:
    source = _netlist(tmp_path)
    plain = Runner(load_config(_write_ofs(tmp_path / "a.ofs", source), TOP))
    policy = Runner(
        load_config(
            _write_ofs(
                tmp_path / "b.ofs", source, "nonscan_cells = u_tap__*\nhold = trst_n:0"
            ),
            TOP,
        )
    )
    assert not {"scan_nonscan_cells", "scan_hold"} & set(
        plain._config_fingerprint_payload()
    )
    payload = policy._config_fingerprint_payload()
    assert payload["scan_nonscan_cells"] == ["u_tap__*"]
    assert payload["scan_hold"] == [["trst_n", 0]]


@pytest.mark.unit
def test_sim_scan_refuses_nonscan_cells_until_they_are_modeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _netlist(tmp_path)
    cfg = load_config(
        _write_ofs(tmp_path / "mini.ofs", source, "nonscan_cells = u_tap__*"), TOP
    )
    cfg.ensure_workspace()
    result = stitch_scan_json(
        source, CELL_MAP, TOP, cfg.scan_json_path, nonscan_cells=cfg.scan.nonscan_cells
    )
    manifest = manifest_from_result(result, source, tmp_path / "map.v", None)
    manifest["latest_check"] = {
        "timestamp": utc_timestamp(),
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "normal_mode": {"vector_count": 0},
        "generic_json_hash": hash_file(cfg.scan_json_path),
    }
    cfg.scan_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RunnerError, match="does not model non-scan cells.*u_tap__st"):
        Runner(cfg)._preflight_sim_scan()
