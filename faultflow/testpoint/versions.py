"""Netlist version stack for add_tp / reject_tp.

State persisted at output/<top>/tp/state.json as:
  {"versions": [{"iter":0,"netlist_path":"...","campaign_id":5,"label":""},...]}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class TPVersion:
    iter: int
    netlist_path: str
    campaign_id: int
    label: str


class TestpointVersionStack:
    def __init__(self, state_path: Path, versions: list[TPVersion]) -> None:
        self._path = state_path
        self._versions = versions

    @classmethod
    def load_or_create(cls, state_path: Path) -> TestpointVersionStack:
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            versions = [TPVersion(**v) for v in data.get("versions", [])]
        else:
            versions = []
        return cls(state_path, versions)

    def push(
        self, netlist_path: Path, campaign_id: int, *, label: str = ""
    ) -> TPVersion:
        n = len(self._versions)
        ver = TPVersion(
            iter=n,
            netlist_path=str(netlist_path),
            campaign_id=campaign_id,
            label=label or (f"tp_iter{n}" if n > 0 else "baseline"),
        )
        self._versions.append(ver)
        self._save()
        return ver

    def pop(self) -> TPVersion:
        if len(self._versions) <= 1:
            raise ValueError("cannot pop: only baseline (iter 0) remains")
        ver = self._versions.pop()
        self._save()
        return ver

    @property
    def current(self) -> TPVersion | None:
        return self._versions[-1] if self._versions else None

    @property
    def baseline(self) -> TPVersion | None:
        return self._versions[0] if self._versions else None

    def all(self) -> list[TPVersion]:
        return list(self._versions)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"versions": [asdict(v) for v in self._versions]}, indent=2)
            + "\n",
            encoding="utf-8",
        )
