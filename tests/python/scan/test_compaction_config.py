from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.runner import Runner


def _cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.ofs"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.unit
def test_compaction_defaults_to_disabled(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    assert cfg.compaction.enabled is False
    assert cfg.compaction.channels == 8


@pytest.mark.unit
def test_compaction_parses_enabled_and_channels(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compaction]\nenabled = true\nchannels = 2\n",
        ),
        "top",
    )
    assert cfg.compaction.enabled is True
    assert cfg.compaction.channels == 2


@pytest.mark.unit
def test_compaction_rejects_nonpositive_channels_when_enabled(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        "[design]\nnetlist = n.json\ncell_lib = c.json\n"
        "[compaction]\nenabled = true\nchannels = 0\n",
    )
    with pytest.raises(ConfigError, match="channels"):
        load_config(cfg, "top")


@pytest.mark.unit
def test_compaction_nonpositive_channels_ignored_when_disabled(tmp_path: Path) -> None:
    # channels=0 would be invalid if compaction were enabled, but validation
    # is gated on enabled -- a disabled section with a nonsense channel count
    # should not block loading a config that never uses it.
    cfg = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compaction]\nenabled = false\nchannels = 0\n",
        ),
        "top",
    )
    assert cfg.compaction.enabled is False
    assert cfg.compaction.channels == 0


@pytest.mark.unit
def test_compaction_independent_of_compression(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compression]\nenabled = true\nchannels = 8\n"
            "[compaction]\nenabled = true\nchannels = 4\n",
        ),
        "top",
    )
    assert cfg.compression.enabled is True
    assert cfg.compression.channels == 8
    assert cfg.compaction.enabled is True
    assert cfg.compaction.channels == 4


@pytest.mark.unit
def test_compaction_settings_are_part_of_the_config_fingerprint(
    tmp_path: Path,
) -> None:
    base = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    enabled = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compaction]\nenabled = true\nchannels = 2\n",
        ),
        "top",
    )
    payload_base = Runner(base)._config_fingerprint_payload()
    payload_enabled = Runner(enabled)._config_fingerprint_payload()

    assert payload_base["compaction_enabled"] is False
    assert payload_enabled["compaction_enabled"] is True
    assert payload_base != payload_enabled
