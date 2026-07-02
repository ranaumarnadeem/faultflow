"""`schemas/coverage.schema.json`'s `policy` object must accept the
`blackbox_instances`/`blackbox_boundary` fields `faultflow/reporter/coverage.py`'s
`_policy()` always emits.

Found via a real bug: the schema had `additionalProperties: false` on `policy` but
never declared those two keys, so EVERY `write_reports()` call failed schema
validation the moment `jsonschema` was actually installed (it had been silently
skipped as an optional dependency up to that point -- see `_validate_report`'s
ImportError soft-skip path). This validates the `policy` sub-schema directly and
in isolation (not the whole top-level report, which has many unrelated required
fields already covered end-to-end by tests/python/cli/test_cli_flow.py's schema
tests and the `unsupported_cells = blackbox` project fixtures).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/coverage.schema.json"


def _policy_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["properties"]["policy"]  # type: ignore[no-any-return]


def _policy(*, blackbox_instances: list[str], blackbox_boundary: str) -> dict:
    return {
        "unsupported_cells": "blackbox" if blackbox_instances else "fail",
        "include_clock_faults": False,
        "include_reset_faults": False,
        "collapsing": False,
        "blackbox_instances": blackbox_instances,
        "blackbox_boundary": blackbox_boundary,
    }


def test_schema_accepts_blackbox_policy_fields() -> None:
    policy = _policy(blackbox_instances=["u_core"], blackbox_boundary="pseudo_port")

    jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_accepts_no_blackbox_policy_fields() -> None:
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")

    jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_rejects_invalid_blackbox_boundary_value() -> None:
    policy = _policy(blackbox_instances=["u_core"], blackbox_boundary="bogus")

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_requires_blackbox_boundary_field() -> None:
    """`_policy()` always emits both keys -- the schema should require them, not
    just tolerate them, so a future regression that drops one is caught."""
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")
    del policy["blackbox_boundary"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())


def test_schema_requires_blackbox_instances_field() -> None:
    policy = _policy(blackbox_instances=[], blackbox_boundary="none")
    del policy["blackbox_instances"]

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(instance=policy, schema=_policy_schema())
