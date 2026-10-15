from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.config import CompressionConfig, ConfigError, load_config
from faultflow.runner import Runner


def _cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.ofs"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.unit
def test_compression_defaults_to_disabled(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    assert cfg.compression.enabled is False
    assert cfg.compression.channels == 8


@pytest.mark.unit
def test_compression_parses_enabled_and_channels(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compression]\nenabled = true\nchannels = 16\n",
        ),
        "top",
    )
    assert cfg.compression.enabled is True
    assert cfg.compression.channels == 16


HEAD = "[design]\nnetlist = n.json\ncell_lib = c.json\n"


def _compression(tmp_path: Path, body: str) -> CompressionConfig:
    return load_config(
        _cfg(tmp_path, HEAD + f"[compression]\nenabled = true\n{body}"), "top"
    ).compression


@pytest.mark.unit
@pytest.mark.parametrize("channels", [1, 3, 8, 24, 64])
def test_compression_takes_any_channel_count_from_1_to_64(
    tmp_path: Path, channels: int
) -> None:
    """The channels are the decompressor's input pins, not the ring's width."""
    assert _compression(tmp_path, f"channels = {channels}\n").channels == channels


@pytest.mark.unit
@pytest.mark.parametrize("channels", [0, -1, 65])
def test_compression_rejects_a_channel_count_outside_1_to_64(
    tmp_path: Path, channels: int
) -> None:
    cfg = _cfg(
        tmp_path, HEAD + f"[compression]\nenabled = true\nchannels = {channels}\n"
    )
    with pytest.raises(ConfigError, match=r"\[compression\] channels"):
        load_config(cfg, "top")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("channels", "ring", "warmup"),
    [
        (1, 8, 8),
        (2, 8, 4),
        (3, 8, 3),
        (8, 16, 2),
        (16, 32, 2),
        (33, 64, 2),
        (64, 64, 1),
    ],
)
def test_the_ring_and_warmup_default_from_the_channels(
    tmp_path: Path, channels: int, ring: int, warmup: int
) -> None:
    """The smallest curated ring at least twice the channels (the widest if none),
    and the shifts a fresh channel bit needs to reach every stage."""
    compression = _compression(tmp_path, f"channels = {channels}\n")
    assert (compression.ring_width, compression.warmup) == (None, None)
    assert (compression.effective_ring_width, compression.effective_warmup) == (
        ring,
        warmup,
    )


@pytest.mark.unit
def test_the_ring_and_warmup_can_be_set(tmp_path: Path) -> None:
    compression = _compression(tmp_path, "channels = 4\nring_width = 32\nwarmup = 5\n")
    assert (compression.effective_ring_width, compression.effective_warmup) == (32, 5)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("channels = 4\nring_width = 24", "ring_width"),  # not a curated width
        ("channels = 16\nring_width = 8", "ring_width"),  # narrower than the channels
        ("channels = 4\nwarmup = 0", "warmup"),
    ],
)
def test_a_bad_ring_or_warmup_is_refused_naming_the_key(
    tmp_path: Path, body: str, key: str
) -> None:
    cfg = _cfg(tmp_path, HEAD + f"[compression]\nenabled = true\n{body}\n")
    with pytest.raises(ConfigError, match=rf"\[compression\] {key}"):
        load_config(cfg, "top")


@pytest.mark.unit
def test_compression_uncurated_channels_ignored_when_disabled(tmp_path: Path) -> None:
    # channels=24 would be invalid if compression were enabled, but validation
    # is gated on enabled -- a disabled section with a nonsense channel count
    # should not block loading a config that never uses it.
    cfg = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compression]\nenabled = false\nchannels = 24\n",
        ),
        "top",
    )
    assert cfg.compression.enabled is False
    assert cfg.compression.channels == 24


@pytest.mark.unit
def test_compression_scan_enable_and_clock_default_to_empty(tmp_path: Path) -> None:
    cfg = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    assert cfg.compression.scan_enable == ""
    assert cfg.compression.clock == ""


@pytest.mark.unit
def test_compression_settings_are_part_of_the_config_fingerprint(
    tmp_path: Path,
) -> None:
    base = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    enabled = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compression]\nenabled = true\nchannels = 8\n",
        ),
        "top",
    )
    payload_base = Runner(base)._config_fingerprint_payload()
    payload_enabled = Runner(enabled)._config_fingerprint_payload()

    assert payload_base["compression_enabled"] is False
    assert payload_enabled["compression_enabled"] is True
    assert payload_base != payload_enabled


@pytest.mark.unit
def test_compressed_atpg_model_is_part_of_the_config_fingerprint(
    tmp_path: Path,
) -> None:
    """A compressed campaign's ATPG loads every pattern from the continuous
    decompressor's channel words (detection_pipeline._decompressed): one graded
    before -- a one-shot seed's loads, or random loads checked after the fact --
    must not resume into it, nor one with another ring or warm-up. Keyed only with
    compression, so an uncompressed design keeps its config_hash."""
    base = load_config(
        _cfg(tmp_path, "[design]\nnetlist = n.json\ncell_lib = c.json\n"), "top"
    )
    enabled = load_config(
        _cfg(
            tmp_path,
            "[design]\nnetlist = n.json\ncell_lib = c.json\n"
            "[compression]\nenabled = true\nchannels = 8\n",
        ),
        "top",
    )
    assert "compression_atpg" not in Runner(base)._config_fingerprint_payload()
    payload = Runner(enabled)._config_fingerprint_payload()
    assert payload["compression_atpg"] == "continuous"
    assert (payload["compression_ring_width"], payload["compression_warmup"]) == (16, 2)

    def payload_of(extra: str) -> dict[str, object]:
        return Runner(
            load_config(
                _cfg(
                    tmp_path,
                    "[design]\nnetlist = n.json\ncell_lib = c.json\n"
                    f"[compression]\nenabled = true\nchannels = 8\n{extra}",
                ),
                "top",
            )
        )._config_fingerprint_payload()

    assert payload_of("warmup = 4\n") != payload
    assert payload_of("ring_width = 32\n") != payload
    assert payload_of("ring_width = 16\nwarmup = 2\n") == payload
