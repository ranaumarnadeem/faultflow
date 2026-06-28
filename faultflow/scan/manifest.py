"""Shared helpers for reading scan-manifest fields.

The manifest is produced by the stitch step; keeping these accessors in one
place avoids the drift that several copy-pasted decode blocks had accumulated.
"""

from __future__ import annotations

from typing import Any

from faultflow.scan.errors import ScanError


def manifest_clock_net_ids(
    manifest: dict[str, Any], *, error_cls: type[Exception] = ScanError
) -> list[int]:
    """Clock-domain net ids from a scan manifest.

    Supports both the v2 schema (``clock_nets``: a list) and the v1 schema
    (``clock_net``: a single int). Raises ``error_cls`` if neither is present
    (callers pass their module's error type to preserve existing behavior).
    """
    clock_nets_raw = manifest.get("clock_nets")
    if isinstance(clock_nets_raw, list) and clock_nets_raw:
        return [int(n) for n in clock_nets_raw]
    clk = manifest.get("clock_net")
    if not isinstance(clk, int):
        raise error_cls("scan manifest must have clock_nets (list) or clock_net (int)")
    return [clk]
