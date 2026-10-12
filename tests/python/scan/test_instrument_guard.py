"""Test instruments are never scan flops by accident (faultflow.scan.instrument_guard).

A JTAG TAP and the IJTAG network are tested through TCK by ``ff.py jtag``, not by scan
ATPG. A flop clocked from the TAP's TCK, one of a JTAG-category instance of the
autoMBIST manifest, or one warptap inserted (``warptap_*``) on a scan chain is refused
by ``ff.py scan`` and ``scan-check`` -- unless ``[scan] scan_test_access = true`` says
the TAP is scan-graded on purpose (``--scan-tap``), which the scan manifest records
(``test_access_scanned``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultflow.config import load_config
from faultflow.runner import Runner, RunnerError
from faultflow.scan.errors import ScanError
from faultflow.scan.instrument_guard import check_test_instruments

ROOT = Path(__file__).resolve().parents[3]
CELL_MAP = ROOT / "cells/sky130/sky130_fd_sc_hd.json"
TOP = "mini"
DFXTP = "sky130_fd_sc_hd__dfxtp_1"


def _module() -> dict[str, Any]:
    ports = {"clk": 2, "tck": 5, "d": 3, "tdi": 7}
    return {"ports": {n: {"direction": "input", "bits": [b]} for n, b in ports.items()}}


def _cells(*rows: tuple[str, int]) -> list[tuple[str, int]]:
    return list(rows)


@pytest.mark.unit
def test_a_flop_clocked_from_tck_on_a_chain_is_refused() -> None:
    cells = _cells(("u0", 2), ("u_tap__st", 5))
    with pytest.raises(ScanError) as exc:
        check_test_instruments(cells, _module(), tck="tck")
    message = str(exc.value)
    assert "u_tap__st" in message and "u0" not in message
    assert "test instruments" in message and "ff.py jtag" in message
    assert "nonscan_cells" in message and "scan_test_access" in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "instance",
    ["warptap_sib_0__ff", "u_core__warptap_tdr_1__q", "jtag_tap__fsm"],
)
def test_a_warptap_or_manifest_instrument_on_a_chain_is_refused_whatever_its_clock(
    instance: str,
) -> None:
    with pytest.raises(ScanError, match=instance):
        check_test_instruments(
            _cells(("u0", 2), (instance, 2)),
            _module(),
            tck="tck",
            globs=("jtag_tap__*",),
        )


@pytest.mark.unit
def test_allowed_it_names_the_instruments_instead_of_refusing() -> None:
    found = check_test_instruments(
        _cells(("u0", 2), ("u_tap__st", 5), ("warptap_sib_0__ff", 2)),
        _module(),
        tck="tck",
        allow=True,
    )
    assert found == ["u_tap__st", "warptap_sib_0__ff"]


@pytest.mark.unit
def test_a_design_without_instruments_passes_and_a_missing_tck_port_is_no_rule() -> (
    None
):
    assert (
        check_test_instruments(_cells(("u0", 2), ("u1", 2)), _module(), tck="tck") == []
    )
    # No port by the TAP's TCK name: a flop on clock net 5 is just a flop.
    assert check_test_instruments(_cells(("u1", 5)), _module(), tck="tms") == []


def _mini_json() -> dict[str, Any]:
    """u0 on clk; u_tap__st, a TAP flop, on tck (not cleared)."""
    ports = {"clk": 2, "d": 3, "q": 4, "tck": 5, "tdi": 7, "tdo": 8}
    outputs = {"q", "tdo"}

    def flop(**pins: int) -> dict[str, Any]:
        return {
            "hide_name": 0,
            "type": DFXTP,
            "parameters": {},
            "attributes": {},
            "port_directions": {p: "output" if p == "Q" else "input" for p in pins},
            "connections": {p: [net] for p, net in pins.items()},
        }

    return {
        "modules": {
            TOP: {
                "attributes": {"top": "1"},
                "ports": {
                    n: {"direction": "output" if n in outputs else "input", "bits": [b]}
                    for n, b in ports.items()
                },
                "cells": {
                    "u0": flop(CLK=2, D=3, Q=4),
                    "u_tap__st": flop(CLK=5, D=7, Q=8),
                },
                "netnames": {
                    n: {"hide_name": 0, "bits": [b], "attributes": {}}
                    for n, b in ports.items()
                },
            }
        }
    }


def _ofs(tmp_path: Path, name: str, scan: str = "", chains: int = 2) -> Path:
    source = tmp_path / "mini.json"
    source.write_text(json.dumps(_mini_json()), encoding="utf-8")
    ofs = tmp_path / name
    ofs.write_text(
        f"[design]\nnetlist = {source}\ncell_lib = {CELL_MAP}\n\n"
        f"[scan]\nchains = {chains}\nrun_techmap = false\n{scan}\n",
        encoding="utf-8",
    )
    return ofs


@pytest.mark.unit
def test_scan_refuses_the_tap_on_a_chain_and_takes_it_when_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(_ofs(tmp_path, "plain.ofs"), TOP)
    with pytest.raises(RunnerError, match=r"test instruments.*u_tap__st"):
        Runner(cfg).scan()

    allowed = load_config(_ofs(tmp_path, "allowed.ofs", "scan_test_access = true"), TOP)
    Runner(allowed).scan()
    manifest = json.loads(allowed.scan_manifest_path.read_text(encoding="utf-8"))
    assert manifest["test_access_scanned"] == ["u_tap__st"]


@pytest.mark.unit
def test_scan_with_the_tap_non_scan_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config(
        _ofs(tmp_path, "nonscan.ofs", "nonscan_cells = u_tap__*", chains=1), TOP
    )
    Runner(cfg).scan()
    manifest = json.loads(cfg.scan_manifest_path.read_text(encoding="utf-8"))
    assert "test_access_scanned" not in manifest
    assert [r["instance"] for r in manifest["cells"]] == ["u0"]


@pytest.mark.unit
def test_scan_check_refuses_a_chain_the_config_doesnt_allow_its_tap_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanned with the TAP on a chain by scan_test_access, then checked by a
    config that no longer says so: scan-check names the instrument."""
    monkeypatch.chdir(tmp_path)
    allowed = load_config(_ofs(tmp_path, "allowed.ofs", "scan_test_access = true"), TOP)
    Runner(allowed).scan()
    assert "scan-check PASS" in Runner(allowed).scan_check(structural_only=True)

    plain = load_config(_ofs(tmp_path, "plain.ofs"), TOP)
    with pytest.raises(RunnerError, match=r"scan-check failed.*u_tap__st"):
        Runner(plain).scan_check(structural_only=True)
