"""Tests for the `retarget` command surface (session method, Tcl, CLI).

These cover the plumbing only — reading exported block patterns, placing them on
the SoC chains via the SoC-access manifest, and writing the result. The
retarget transform itself is covered by tests/python/project/test_soc_retarget.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultflow.cli import main
from faultflow.retarget.emit import dict_to_pattern, read_retargeted
from faultflow.scan.pattern_export import scan_pattern_to_dict
from faultflow.scan.protocol import ScanPattern
from faultflow.shell.errors import ShellError
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge

try:
    from tests.python.cli.test_shell_core import FakeService  # type: ignore
except Exception:  # pragma: no cover - import path fallback
    FakeService = None  # type: ignore


SOC_ACCESS = {
    "schema": "faultflow_soc_access_v1",
    "assembly_top": "blkB",
    "soc_scan": {
        "scan_inputs": ["wbr_si"],
        "scan_outputs": ["soc_so"],
        "scan_enable": "wbr_se",
        "clock_ports": ["CLK"],
        "max_chain_length": 4,
    },
    "soc_chains": [
        {
            "index": 0,
            "scan_in": "wbr_si",
            "scan_out": "soc_so",
            "length": 4,
            "segments": [
                {"block": "blkB", "block_chain": 0, "soc_offset": 0, "length": 3},
                {
                    "block": "__bypass",
                    "block_chain": None,
                    "soc_offset": 3,
                    "length": 1,
                },
            ],
        }
    ],
    "wrapper_instruction": {"blkB": "INTEST"},
}


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    pattern = ScanPattern(
        load_seqs={0: [True, False, True]},
        capture_pi_values={"a": True, "b": False},
        expected_unload={0: [False, True, True]},
    )
    patterns_path = tmp_path / "blkB_patterns.json"
    patterns_path.write_text(json.dumps([scan_pattern_to_dict(pattern)]))
    soc_path = tmp_path / "soc_access.json"
    soc_path.write_text(json.dumps(SOC_ACCESS))
    out_path = tmp_path / "retargeted.json"
    return patterns_path, soc_path, out_path


def _session(tmp_path: Path) -> ProjectSession:
    service = FakeService() if FakeService is not None else None
    return ProjectSession(output_root=tmp_path / "output", service=service)


def test_session_retarget_writes_readable_output(tmp_path: Path) -> None:
    patterns_path, soc_path, out_path = _write_inputs(tmp_path)
    session = _session(tmp_path)

    result = session.retarget(
        patterns=patterns_path, soc_access=soc_path, block="blkB", out=out_path
    )
    assert "retargeted 1 pattern" in result.message

    payload = read_retargeted(out_path)
    assert payload["source_block"] == "blkB"
    assert payload["assembly_top"] == "blkB"
    assert len(payload["patterns"]) == 1
    # Each entry round-trips through the retargeted-pattern reader.
    soc_pattern = dict_to_pattern(payload["patterns"][0])
    assert soc_pattern.source_block == "blkB"


def test_tcl_retarget_returns_ok(tmp_path: Path) -> None:
    patterns_path, soc_path, out_path = _write_inputs(tmp_path)
    bridge = TclBridge(_session(tmp_path))

    out = bridge.eval(
        f"retarget -patterns {patterns_path} -soc_access {soc_path} "
        f"-block blkB -o {out_path}"
    )
    assert out_path.exists()
    assert out is not None


def test_tcl_retarget_missing_block_errors(tmp_path: Path) -> None:
    patterns_path, soc_path, out_path = _write_inputs(tmp_path)
    bridge = TclBridge(_session(tmp_path))
    with pytest.raises(ShellError) as exc:
        bridge.call(
            "retarget",
            "-patterns",
            str(patterns_path),
            "-soc_access",
            str(soc_path),
            "-o",
            str(out_path),
        )
    assert "MISSING_ARG" in str(exc.value) or "required" in str(exc.value)


def test_cli_retarget(tmp_path: Path) -> None:
    patterns_path, soc_path, out_path = _write_inputs(tmp_path)
    rc = main(
        [
            "retarget",
            "--patterns",
            str(patterns_path),
            "--soc-access",
            str(soc_path),
            "--block",
            "blkB",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    assert read_retargeted(out_path)["source_block"] == "blkB"


def test_cli_retarget_missing_patterns_file_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A nonexistent --patterns file must give a clean CLI error (exit 2), not a
    raw FileNotFoundError traceback -- matching how --soc-access is guarded."""
    _patterns, soc_path, out_path = _write_inputs(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "retarget",
                "--patterns",
                str(tmp_path / "does_not_exist.json"),
                "--soc-access",
                str(soc_path),
                "--block",
                "blkB",
                "--out",
                str(out_path),
            ]
        )
    assert exc.value.code == 2
    assert "patterns file not found" in capsys.readouterr().err


def test_cli_retarget_malformed_patterns_file_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patterns, soc_path, out_path = _write_inputs(tmp_path)
    bad = tmp_path / "bad_patterns.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "retarget",
                "--patterns",
                str(bad),
                "--soc-access",
                str(soc_path),
                "--block",
                "blkB",
                "--out",
                str(out_path),
            ]
        )
    assert exc.value.code == 2
    assert "not valid JSON" in capsys.readouterr().err
