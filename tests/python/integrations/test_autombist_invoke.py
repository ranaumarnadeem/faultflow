"""Tests for `faultflow.integrations.autombist.invoke_autombist_generate`.

Pure unit tests via `monkeypatch.setattr(subprocess, "run", ...)` -- no real
autoMBIST invocation, no external tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultflow.integrations.autombist import (
    AutombistRunError,
    invoke_autombist_generate,
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
