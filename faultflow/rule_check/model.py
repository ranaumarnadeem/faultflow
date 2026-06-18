"""Data model for the DFT rule checker: severities, violations, report."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Violation:
    rule_id: str  # stable id, e.g. "CLK001"
    severity: Severity
    title: str  # short rule name
    message: str  # specific instance detail

    def format_line(self) -> str:
        return f"[{self.severity.value.upper()}] {self.rule_id} {self.title}: {self.message}"


@dataclass
class RuleCheckReport:
    top: str
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.INFO]

    def passed(self, strict: bool = False) -> bool:
        """No blocking violations. In strict mode warnings also block."""
        if self.errors:
            return False
        if strict and self.warnings:
            return False
        return True
