"""Tests for `faultflow.integrations.autombist.invoke_autombist_generate` and
`invoke_autombist_wrap_test_access`.

Pure unit tests via `monkeypatch.setattr(subprocess, "run", ...)` -- no real
autoMBIST invocation, no external tools.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultflow.integrations.autombist import (
    AutombistRunError,
    invoke_autombist_generate,
    invoke_autombist_wrap_test_access,
    run_autombist_generate,
)


def _fake_run(returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return run, calls


def test_invokes_expected_argv_with_default_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stem = tmp_path / "out" / "mydesign"
    stem.mkdir(parents=True)
    (stem / "manifest.json").write_text("{}", encoding="utf-8")

    run, calls = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    result = invoke_autombist_generate(tmp_path / "cfg.yml", tmp_path / "out")

    assert result == stem / "manifest.json"
    assert calls == [
        [
            "autombist",
            "generate",
            "--config",
            str(tmp_path / "cfg.yml"),
            "--out",
            str(tmp_path / "out"),
            "--emit-manifest",
        ]
    ]


def test_invokes_expected_argv_with_custom_cmd_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stem = tmp_path / "out" / "mydesign"
    stem.mkdir(parents=True)
    (stem / "manifest.json").write_text("{}", encoding="utf-8")

    run, calls = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    invoke_autombist_generate(
        tmp_path / "cfg.yml",
        tmp_path / "out",
        cmd=("python3", "-m", "autombist.cli"),
    )

    assert calls[0][:3] == ["python3", "-m", "autombist.cli"]


def test_nonzero_exit_raises_with_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _fake_run(returncode=1, stderr="bad config: missing memory_name")
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(AutombistRunError, match="missing memory_name"):
        invoke_autombist_generate(tmp_path / "cfg.yml", tmp_path / "out")


def test_zero_manifest_matches_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "out").mkdir()
    run, _ = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(AutombistRunError, match="no \\*/manifest.json"):
        invoke_autombist_generate(tmp_path / "cfg.yml", tmp_path / "out")


def test_multiple_manifest_matches_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b"):
        d = tmp_path / "out" / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text("{}", encoding="utf-8")

    run, _ = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(AutombistRunError, match="multiple manifest.json"):
        invoke_autombist_generate(tmp_path / "cfg.yml", tmp_path / "out")


def test_wrap_test_access_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    invoke_autombist_wrap_test_access(
        tmp_path / "out" / "mydesign", cmd=("python3", "-m", "autombist")
    )

    assert calls == [
        [
            "python3",
            "-m",
            "autombist",
            "wrap-test-access",
            "--manifest",
            str(tmp_path / "out" / "mydesign"),
            "--emit-icl",
        ]
    ]


def test_wrap_test_access_failure_raises_with_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = _fake_run(returncode=1, stderr="autombist: warptap is not installed")
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(AutombistRunError, match="warptap is not installed"):
        invoke_autombist_wrap_test_access(tmp_path / "out" / "mydesign")


def test_test_access_runs_wrap_after_generate_and_needs_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--test-access wraps the design generate wrote. A wrap that exits 0 but
    records no wrapped test_access block must not fall back to synthesizing
    the unwrapped design."""
    stem = tmp_path / "out" / "mydesign"
    stem.mkdir(parents=True)
    (stem / "wrapper.v").write_text("module top(); endmodule\n", encoding="utf-8")
    (stem / "manifest.json").write_text(
        json.dumps(
            {
                "format": "autombist_instance_manifest",
                "schema_version": "1.0.0",
                "top_module": "top",
                "sources": {"wrapper": "wrapper.v"},
                "instances": [
                    {
                        "category": "memory",
                        "hierarchical_path": "u_mem",
                        "hierarchy_hint": "blackbox",
                        "instance_name": "u_mem",
                        "module_type": "mem",
                        "sources": ["mem_bbox.v"],
                    }
                ],
                "test_access": None,
            }
        ),
        encoding="utf-8",
    )
    run, calls = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(AutombistRunError, match="no wrapped test_access block"):
        run_autombist_generate(
            tmp_path / "cfg.yml",
            tmp_path / "out",
            liberty=tmp_path / "lib.lib",
            cell_lib=tmp_path / "cells.json",
            test_access=True,
        )
    assert [call[1] for call in calls] == ["generate", "wrap-test-access"]
    assert calls[1][2:] == ["--manifest", str(stem), "--emit-icl"]
