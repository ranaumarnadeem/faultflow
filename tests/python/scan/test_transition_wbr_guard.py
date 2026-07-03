"""The transition-ATPG scan-WBR guard must reject only genuinely wrapped designs.

The 1-FF scan WBC clobbers q on a second capture, so a scan-WBR-wrapped block
cannot run LOC/LOS transition ATPG. The guard used to fire on the `wbr_model`
config alone -- which defaults to "scan" -- so it wrongly rejected transition ATPG
on a plain, UNWRAPPED scan design that has no WBR cells (found in the e2e bug hunt).
It must key off whether the design ACTUALLY has scan WBR cells (a non-empty
manifest `wrapper_chains`), not the config default.
"""

from __future__ import annotations

from typing import Any

import pytest

from faultflow.scan.detection_pipeline import _guard_transition_on_scan_wbr
from faultflow.scan.errors import ScanError


def _manifest(*, wrapped: bool) -> dict[str, Any]:
    # A scan-WBR-wrapped block's manifest carries a non-empty wrapper_chains; a
    # plain scan design (and the buffer model) has an empty one.
    return {"wrapper_chains": [{"index": 0, "cells": ["__wi_a"]}] if wrapped else []}


def test_rejects_transition_on_scan_wrapped_design() -> None:
    with pytest.raises(ScanError, match="does not support transition"):
        _guard_transition_on_scan_wbr("scan", _manifest(wrapped=True), transition=True)


def test_allows_transition_on_plain_unwrapped_scan_design() -> None:
    # The actual bug: default wbr_model="scan" but no WBR cells -> must NOT raise.
    _guard_transition_on_scan_wbr("scan", _manifest(wrapped=False), transition=True)
    # A manifest without the key at all is also treated as unwrapped.
    _guard_transition_on_scan_wbr("scan", {}, transition=True)


def test_allows_non_transition_on_scan_wrapped_design() -> None:
    # INTEST (non-transition) on a scan-WBR block is the supported case.
    _guard_transition_on_scan_wbr("scan", _manifest(wrapped=True), transition=False)


def test_allows_transition_under_buffer_model_even_when_wrapped() -> None:
    _guard_transition_on_scan_wbr("buffer", _manifest(wrapped=True), transition=True)
