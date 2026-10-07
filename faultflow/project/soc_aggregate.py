"""One chip number from a faultflow_project_v2 SoC's tests: each block's INTEST and
the SoC's EXTEST, faults counted once by their identities.

A block's INTEST grades the block's own fault sites; the SoC's EXTEST grades the
SoC's, each of which is the block faults it stands for, or a glue fault
(faultflow.project.identity). Each scope owns what its own test grades -- a fault in
its coverage denominator -- and hands the faults the other mode owns to it
(``wbr_decoupled``, faultflow.wrap.sides). For the chip number to be right, three
guards hold, per identity:
1. the scopes are distinct;
2. no identity is owned by two scopes: no fault counted twice;
3. every identity a scope hands off is owned by another, or accounted for there --
   excluded by design (clock, reset, scan path), collapsed into an equivalent fault,
   or proven redundant, as a flat run of the whole chip would leave it -- or tied
   to a constant by the glue, so the SoC has no such site at all.
A fault of the SoC with several identities (a wire between two blocks' ports is
each block's port stem) is one fault: it counts once.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection

from faultflow.db import connect, latest_campaign_id
from faultflow.project.identity import Identity


class AggregateError(RuntimeError):
    """A guard failed: the chip number would be wrong."""


@dataclass(frozen=True)
class Scope:
    """A scope's campaign and how its fault sites' identities are found: a block's
    are its own (``(name, key)``), the SoC's through the composition."""

    name: str
    db_path: Path
    campaign_type: str
    identities: Callable[[str], list[Identity]]


@dataclass(frozen=True)
class ScopeCoverage:
    name: str
    campaign_id: int
    owned: int  # faults in this scope's denominator
    owned_detected: int
    handoff: int  # left to the other mode (wbr_decoupled)
    excluded: int  # by design, collapsed, or redundant
    total: int

    @property
    def coverage_percent(self) -> float | None:
        return 100.0 * self.owned_detected / self.owned if self.owned else None


@dataclass
class ChipCoverage:
    project: str
    chip_denominator: int = 0
    chip_detected: int = 0
    scopes: list[ScopeCoverage] = field(default_factory=list)
    guards: dict[str, Any] = field(default_factory=dict)

    @property
    def chip_coverage_percent(self) -> float | None:
        if not self.chip_denominator:
            return None
        return 100.0 * self.chip_detected / self.chip_denominator


def _rows(scope: Scope) -> tuple[int, list[sqlite3.Row]]:
    if not scope.db_path.exists():
        raise AggregateError(f"scope {scope.name}: no database at {scope.db_path}")
    with connect(scope.db_path) as conn:
        conn.row_factory = sqlite3.Row
        campaign = latest_campaign_id(conn, scope.campaign_type)
        if campaign is None:
            raise AggregateError(f"scope {scope.name}: no {scope.campaign_type} run")
        rows = conn.execute(
            "SELECT fault_site_key, lower(fault_type) AS fault_type, status, "
            "exclusion, collapsed_into FROM faults WHERE campaign_id = ?",
            (campaign,),
        ).fetchall()
    return campaign, list(rows)


def aggregate_soc(
    project: str, scopes: list[Scope], *, tied: Collection[Identity] = ()
) -> ChipCoverage:
    """The chip number over `scopes`, the guards enforced. `tied`: block identities
    the glue ties to a constant, which have no site in the SoC."""
    names = [scope.name for scope in scopes]
    if not scopes or len(set(names)) != len(names):
        raise AggregateError(f"the scopes aren't distinct: {names}")
    chip = ChipCoverage(project)
    owners: dict[tuple[Identity, str], str] = {}
    handed: dict[tuple[Identity, str], str] = {}
    accounted: set[tuple[Identity, str]] = {
        (identity, t) for identity in tied for t in ("sa0", "sa1")
    }
    twice: list[str] = []
    for scope in scopes:
        campaign, rows = _rows(scope)
        owned = detected = handoff = excluded = 0
        for row in rows:
            key, fault_type = str(row["fault_site_key"]), str(row["fault_type"])
            ids = [(identity, fault_type) for identity in scope.identities(key)]
            counted = (
                row["exclusion"] == "none"
                and row["status"] != "redundant"
                and row["collapsed_into"] is None
            )
            if counted:
                owned += 1
                detected += row["status"] == "detected"
                for found in ids:
                    previous = owners.setdefault(found, scope.name)
                    if previous != scope.name:
                        twice.append(f"{found} in {previous} and {scope.name}")
            elif row["exclusion"] == "wbr_decoupled":
                handoff += 1
                for found in ids:
                    handed.setdefault(found, scope.name)
            else:
                excluded += 1
                accounted.update(ids)
        chip.scopes.append(
            ScopeCoverage(
                scope.name, campaign, owned, detected, handoff, excluded, len(rows)
            )
        )
        chip.chip_denominator += owned
        chip.chip_detected += detected
    if twice:
        raise AggregateError(f"faults counted by two scopes: {sorted(twice)[:10]}")
    unowned = sorted(
        f"{identity} {fault_type} (left by {handed[(identity, fault_type)]})"
        for identity, fault_type in handed
        if (identity, fault_type) not in owners
        and (identity, fault_type) not in accounted
    )
    if unowned:
        raise AggregateError(
            "faults one scope leaves to another, which no other scope grades: the "
            f"chip number would leave them out: {unowned[:10]}"
        )
    if chip.chip_denominator <= 0:
        raise AggregateError("the chip has no fault to cover")
    chip.guards = {
        "scopes_distinct": True,
        "no_fault_counted_twice": True,
        "handoffs_owned": True,
        "owned_identities": len(owners),
        "handed_identities": len(handed),
    }
    return chip
