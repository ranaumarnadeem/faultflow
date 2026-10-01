"""The facts mbist-insert needs from a memory's autoMBIST config: the memory
macro, the collar's module name, and which macro pin plays which role.

Only a dedicated, single-port collar can stand in for a memory: one controller
for one memory, its six roles on six pins. A shared-bus config (several memories
behind one controller) or a named multi-port config is refused. autoMBIST itself
validates the rest when it generates the collar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.config import ConfigError

ROLES = ("clk", "addr", "din", "dout", "we", "csb")


class AutombistConfigError(ConfigError):
    """An autoMBIST config mbist-insert can't use."""


@dataclass(frozen=True)
class CollarConfig:
    path: Path
    memory_name: str  # the macro module
    wrapper_module_name: str  # the collar module autoMBIST generates
    addr_width: int
    data_width: int
    we_active_low: bool
    pins: dict[str, str]  # role -> macro pin, for every role in ROLES

    @property
    def role_pins(self) -> frozenset[str]:
        return frozenset(self.pins.values())


def load_collar_config(path: Path) -> CollarConfig:
    try:
        import yaml
    except ImportError as exc:
        raise AutombistConfigError(
            f"{path}: reading an autoMBIST config needs PyYAML (pip install pyyaml)"
        ) from exc
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AutombistConfigError(f"cannot read autoMBIST config {path}: {exc}")
    if not isinstance(data, dict):
        raise AutombistConfigError(f"{path}: an autoMBIST config is a mapping")
    if data.get("topology", "dedicated") != "dedicated" or "memories" in data:
        raise AutombistConfigError(
            f"{path}: a shared-bus config tests several memories through one "
            "controller; mbist-insert wraps one memory per collar (topology: "
            "dedicated)"
        )
    ports = data.get("ports")
    if not isinstance(ports, dict) or any(isinstance(v, dict) for v in ports.values()):
        raise AutombistConfigError(
            f"{path}: mbist-insert needs the flat single-port form of ports: "
            f"({', '.join(ROLES)}), not named ports"
        )
    missing = [role for role in ROLES if not isinstance(ports.get(role), str)]
    if missing:
        raise AutombistConfigError(f"{path}: ports is missing {missing}")
    pins = {role: str(ports[role]) for role in ROLES}
    if len(set(pins.values())) != len(pins):
        raise AutombistConfigError(f"{path}: two roles share a pin: {pins}")
    for key in ("memory_name", "wrapper_module_name"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise AutombistConfigError(f"{path}: {key} is required")
    for key in ("addr_width", "data_width"):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AutombistConfigError(f"{path}: {key} must be a positive integer")
    if not isinstance(data.get("we_active_low"), bool):
        raise AutombistConfigError(f"{path}: we_active_low must be true or false")
    return CollarConfig(
        path=path,
        memory_name=data["memory_name"],
        wrapper_module_name=data["wrapper_module_name"],
        addr_width=data["addr_width"],
        data_width=data["data_width"],
        we_active_low=data["we_active_low"],
        pins=pins,
    )
