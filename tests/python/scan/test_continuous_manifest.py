"""The continuous decompressor as the scan manifest records it
(``ContinuousDecompressor.manifest_entry`` / ``from_manifest``): everything a later
step -- scan-compact composing the whole chip, the ATPG, warptap retargeting a
pattern -- needs to rebuild it, and a refusal for a manifest from before (a one-shot
decompressor's entry has no architecture)."""

from __future__ import annotations

import pytest

from faultflow.scan.compression import (
    ContinuousDecompressor,
    build_continuous_decompressor,
)
from faultflow.scan.errors import ScanError
from faultflow.scan.ring_generator import PRIMITIVE_POLYNOMIALS

PORTS = [f"scan_in_{k}" for k in range(5)]


def _decompressor(**over):  # type: ignore[no-untyped-def]
    return build_continuous_decompressor(
        PORTS,
        3,
        clock_port="clk",
        scan_enable_port="scan_en",
        channel_port="tdi",
        **over,
    )


@pytest.mark.unit
def test_the_builder_defaults_the_ring_and_warmup_from_the_channels():
    dec = _decompressor()
    assert dec.polynomial == PRIMITIVE_POLYNOMIALS[8]  # 3 channels: the 8-stage ring
    assert dec.warmup == 3  # ceil(8 / 3) shifts for a channel bit to reach every stage
    assert dec.scan_in_ports == PORTS and len(dec.phase_shifter_taps) == 5
    assert (dec.channel_port, dec.scan_enable_port, dec.clock_port) == (
        "tdi",
        "scan_en",
        "clk",
    )


@pytest.mark.unit
def test_the_builder_takes_a_ring_and_a_warmup():
    dec = _decompressor(ring_width=32, warmup=6)
    assert (dec.polynomial.width, dec.warmup) == (32, 6)


@pytest.mark.unit
def test_a_ring_narrower_than_its_channels_is_refused():
    with pytest.raises(ValueError, match="channels"):
        build_continuous_decompressor(PORTS, 16, ring_width=8)


@pytest.mark.unit
def test_the_manifest_entry_names_what_a_later_step_needs():
    entry = _decompressor().manifest_entry()
    assert entry["architecture"] == "continuous"
    assert (entry["num_channels"], entry["ring_width"], entry["warmup"]) == (3, 8, 3)
    assert entry["injectors"] == [0, 2, 5]
    assert entry["scan_in_ports"] == PORTS
    assert entry["polynomial"] == {"width": 8, "taps": [4, 5, 6]}
    assert entry["tap_source_net"] == "effective_state"
    assert (entry["channel_port"], entry["clock_port"]) == ("tdi", "clk")
    assert entry["scan_enable_port"] == "scan_en"
    assert len(entry["phase_shifter_taps"]) == 5


@pytest.mark.unit
def test_a_decompressor_read_back_from_its_entry_is_the_decompressor():
    dec = _decompressor(ring_width=16, warmup=4)
    assert ContinuousDecompressor.from_manifest(dec.manifest_entry()) == dec


@pytest.mark.unit
@pytest.mark.parametrize("architecture", [None, "one_shot"])
def test_a_manifest_from_a_one_shot_decompressor_says_to_run_scan_compress_again(
    architecture,
):
    entry = _decompressor().manifest_entry()
    del entry["architecture"]
    if architecture is not None:
        entry["architecture"] = architecture
    with pytest.raises(ScanError, match="scan-compress"):
        ContinuousDecompressor.from_manifest(entry)


@pytest.mark.unit
def test_injectors_the_manifest_states_must_be_the_models():
    entry = _decompressor().manifest_entry()
    entry["injectors"] = [0, 1, 2]
    with pytest.raises(ScanError, match="injectors"):
        ContinuousDecompressor.from_manifest(entry)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key", ["warmup", "polynomial", "phase_shifter_taps", "num_channels"]
)
def test_a_manifest_missing_a_field_is_refused_naming_it(key):
    entry = _decompressor().manifest_entry()
    del entry[key]
    with pytest.raises(ScanError, match=key):
        ContinuousDecompressor.from_manifest(entry)
