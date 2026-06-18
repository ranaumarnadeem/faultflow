"""DFT rule checker (DRC) for faultflow.

A standalone structural rule checker over the synthesized netlist. It reuses the
existing scan structural checks and FF eligibility, and adds the structural rules
the IR did not previously surface (combinational feedback, uncontrollable clock,
clock-as-data). It is invoked explicitly via the `rule_check` command and never
auto-triggers before sim/sim --scan.
"""

from __future__ import annotations

from faultflow.rule_check.model import RuleCheckReport, Severity, Violation
from faultflow.rule_check.rules import run_rule_check

__all__ = [
    "RuleCheckReport",
    "Severity",
    "Violation",
    "run_rule_check",
]
