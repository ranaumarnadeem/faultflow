from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SiteProvenance:
    """Typed site provenance exported from C++ compilation."""

    yosys_net_id: int
    kind: str  # stem | branch | pseudo
    consumer_instance: str = ""
    input_pin: str = ""
    pseudo_role: str = ""  # ppi | ppo when kind=pseudo

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SiteProvenance:
        return cls(
            yosys_net_id=int(raw["yosys_net_id"]),
            kind=str(raw["kind"]),
            consumer_instance=str(raw.get("consumer_instance", "")),
            input_pin=str(raw.get("input_pin", "")),
            pseudo_role=str(raw.get("pseudo_role", "")),
        )


def canonical_site_key(prov: SiteProvenance) -> str:
    """Sole JSON canonicalizer for fault site identity."""
    if prov.kind == "stem":
        return f"net:{prov.yosys_net_id}:stem"
    if prov.kind == "branch":
        if not prov.consumer_instance or not prov.input_pin:
            raise ValueError("branch site requires consumer_instance and input_pin")
        return (
            f"net:{prov.yosys_net_id}:branch:"
            f"{prov.consumer_instance}:{prov.input_pin}"
        )
    if prov.kind == "pseudo":
        if not prov.consumer_instance or not prov.pseudo_role:
            raise ValueError("pseudo site requires consumer_instance and pseudo_role")
        return f"pseudo:{prov.pseudo_role}:{prov.consumer_instance}"
    raise ValueError(f"unknown site kind: {prov.kind}")


def stem_site_key(yosys_net_id: int) -> str:
    """Interim helper until C++ provenance export is wired everywhere."""
    return canonical_site_key(SiteProvenance(yosys_net_id=yosys_net_id, kind="stem"))
