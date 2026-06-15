from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class OperationResult:
    operation: str
    top: str
    message: str
    artifacts: Mapping[str, Path] = field(default_factory=dict)
    metrics: Mapping[str, Scalar] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanResult(OperationResult):
    removed: int = 0


@dataclass(frozen=True)
class InitializationResult(OperationResult):
    pass


@dataclass(frozen=True)
class SynthesisResult(OperationResult):
    synthesized: bool = True
    reason: str = ""


@dataclass(frozen=True)
class ScanInsertionResult(OperationResult):
    pass


@dataclass(frozen=True)
class ScanCheckResult(OperationResult):
    passed: bool = True


@dataclass(frozen=True)
class AtpgResult(OperationResult):
    pass


@dataclass(frozen=True)
class CampaignStatusResult(OperationResult):
    campaign_state: str = "not_run"


@dataclass(frozen=True)
class NetlistWriteResult(OperationResult):
    pass


@dataclass(frozen=True)
class ReportResult(OperationResult):
    pass
