"""The error a netlist that can't be wrapped raises."""

from __future__ import annotations

from faultflow.config import ConfigError


class WrapError(ConfigError):
    """A netlist that cannot be wrapped (missing top, bad ports, etc.)."""
