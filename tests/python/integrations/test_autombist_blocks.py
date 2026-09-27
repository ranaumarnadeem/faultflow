"""Tests for `faultflow.integrations.autombist.plan_blocks`.

Pure unit tests, no file I/O -- `AutombistInstance`/`AutombistManifest` are
constructed directly in Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.integrations.autombist import (
    AutombistInstance,
    AutombistManifest,
    AutombistManifestError,
    plan_blocks,
)


def _instance(
    *,
    hierarchical_path: str,
    hierarchy_hint: str = "separate",
    module_type: str = "algo_top",
    sources: tuple[Path, ...] = (Path("/a/algo.sv"),),
    parameters: dict | None = None,
) -> AutombistInstance:
    return AutombistInstance(
        category="mbist_controller",
        hierarchical_path=hierarchical_path,
        hierarchy_hint=hierarchy_hint,
        instance_name=hierarchical_path,
        module_type=module_type,
        sources=sources,
        parameters=parameters or {},
    )


def _manifest(instances: tuple[AutombistInstance, ...]) -> AutombistManifest:
    return AutombistManifest(
        top_module="top", wrapper=Path("/a/top.v"), instances=instances
    )


def test_two_instances_same_module_and_params_fold_into_one_block() -> None:
    manifest = _manifest(
        (
            _instance(hierarchical_path="u_a", parameters={"W": 4}),
            _instance(hierarchical_path="u_b", parameters={"W": 4}),
        )
    )
    blocks = plan_blocks(manifest)
    assert len(blocks) == 1
    assert blocks[0].module == "algo_top"
    assert blocks[0].parameters == {"W": 4}
    assert blocks[0].instance_paths == ("u_a", "u_b")


def test_conflicting_parameters_for_same_module_type_raises() -> None:
    manifest = _manifest(
        (
            _instance(hierarchical_path="u_a", parameters={"W": 4}),
            _instance(hierarchical_path="u_b", parameters={"W": 8}),
        )
    )
    with pytest.raises(AutombistManifestError, match="different.*parameter"):
        plan_blocks(manifest)


def test_conflicting_sources_for_same_module_type_raises() -> None:
    manifest = _manifest(
        (
            _instance(hierarchical_path="u_a", sources=(Path("/a/v1.sv"),)),
            _instance(hierarchical_path="u_b", sources=(Path("/a/v2.sv"),)),
        )
    )
    with pytest.raises(AutombistManifestError, match="different.*source"):
        plan_blocks(manifest)


def test_blackbox_instances_excluded() -> None:
    manifest = _manifest(
        (
            _instance(hierarchical_path="u_mem", hierarchy_hint="blackbox"),
            _instance(hierarchical_path="u_algo"),
        )
    )
    blocks = plan_blocks(manifest)
    assert len(blocks) == 1
    assert blocks[0].instance_paths == ("u_algo",)


def test_distinct_module_types_produce_distinct_blocks() -> None:
    manifest = _manifest(
        (
            _instance(hierarchical_path="u_a", module_type="algo_a"),
            _instance(hierarchical_path="u_b", module_type="algo_b"),
        )
    )
    blocks = plan_blocks(manifest)
    assert len(blocks) == 2
    assert {b.module for b in blocks} == {"algo_a", "algo_b"}


def test_no_separate_instances_returns_empty() -> None:
    manifest = _manifest(
        (_instance(hierarchical_path="u_mem", hierarchy_hint="blackbox"),)
    )
    assert plan_blocks(manifest) == ()
