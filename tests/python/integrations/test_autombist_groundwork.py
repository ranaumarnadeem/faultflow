"""Groundwork for inserting MBIST into a user's design: literal non-scan globs,
Yosys `check` problems parsed against a baseline, and the IJTAG instrument specs a
network is built from."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultflow.integrations.autombist import (
    AutombistManifest,
    AutombistManifestError,
    AutombistTestAccess,
    AutombistTestAccessInstance,
    CheckProblem,
    literal_glob,
    parse_check_problems,
    tap_nonscan_settings,
)
from faultflow.project.assemble import AssembleError
from warptap_helpers import skip_unless_warptap

pytestmark = pytest.mark.unit


# --- literal globs -----------------------------------------------------------


def test_a_generate_path_matches_itself_not_its_bracket_free_twin() -> None:
    glob = literal_glob("u_core.g[0].u_tap") + "__*"
    assert fnmatchcase("u_core.g[0].u_tap__dff_3", glob)
    assert not fnmatchcase("u_core.g0.u_tap__dff_3", glob)


def test_every_glob_metacharacter_is_literal() -> None:
    name = "a*b?c[d]e"
    assert fnmatchcase(name, literal_glob(name))
    for other in ("aXbYcde", "a*b?cde", "a*b?c[d]eX"):
        assert not fnmatchcase(other, literal_glob(name))


def test_a_plain_path_is_unchanged() -> None:
    assert literal_glob("u_core.u_mem") == "u_core.u_mem"


def _wrapped(*instances: tuple[str, str]) -> AutombistManifest:
    access = AutombistTestAccess(
        top_module="chip",
        output_verilog=Path("chip_wrapped.v"),
        boundary_ports=(),
        instances=tuple(
            AutombistTestAccessInstance(
                category=category,
                hierarchical_path=path,
                hierarchy_hint="separate",
                instance_name=path.rsplit(".", 1)[-1],
                module_type="m",
            )
            for path, category in instances
        ),
        instruments=(),
    )
    return AutombistManifest(
        top_module="chip", wrapper=Path("chip.v"), instances=(), test_access=access
    )


def test_tap_nonscan_globs_match_their_instances_literally() -> None:
    manifest = _wrapped(
        ("u_core.g[0].u_tap", "jtag_tap"),
        ("u_sib_0", "ijtag_sib"),
        ("u_core.g[0].u_ctl", "mbist_controller"),
    )
    globs, holds = tap_nonscan_settings(manifest)
    assert globs == ("u_core.g[[]0].u_tap__*", "u_sib_0__*")
    assert holds == (("trst_n", 0), ("tck", 0))
    assert any(fnmatchcase("u_core.g[0].u_tap__tap_fsm_q", g) for g in globs)
    assert not any(fnmatchcase("u_core.g[0].u_ctl__state_q", g) for g in globs)


def test_tap_nonscan_needs_a_design_wrapped_for_jtag() -> None:
    manifest = AutombistManifest(
        top_module="chip", wrapper=Path("chip.v"), instances=()
    )
    with pytest.raises(AutombistManifestError, match="test_access"):
        tap_nonscan_settings(manifest)


# --- Yosys check problems ----------------------------------------------------

# A real Yosys 0.61 log of `check` on a design with two drivers on one net and
# two used-but-undriven wires, one inside a submodule.
CHECK_LOG = """\
3. Executing PROC pass (convert processes to netlists).
Warning: Not a CHECK problem: an earlier pass's warning.
4. Executing CHECK pass (checking for obvious problems).
Checking module t...
Warning: multiple conflicting drivers for t.\\b:
    module input b[0]
    module input a[0]
Warning: Wire t.\\undriven_bus is used but has no driver.
Checking module leaf...
Warning: Wire leaf.\\floating is used but has no driver.
Found and reported 3 problems.

Warnings: 4 unique messages, 4 total
End of script.
"""


def test_check_problems_are_parsed_with_their_details() -> None:
    assert parse_check_problems(CHECK_LOG) == (
        CheckProblem(
            "multiple conflicting drivers for t.\\b:",
            ("module input b[0]", "module input a[0]"),
        ),
        CheckProblem("Wire t.\\undriven_bus is used but has no driver."),
        CheckProblem("Wire leaf.\\floating is used but has no driver."),
    )


def test_a_clean_check_has_no_problems() -> None:
    log = (
        "4. Executing CHECK pass (checking for obvious problems).\n"
        "Checking module t...\n"
        "Found and reported 0 problems.\n"
    )
    assert parse_check_problems(log) == ()


def test_a_problem_count_the_parser_cant_account_for_raises() -> None:
    log = CHECK_LOG.replace("reported 3 problems", "reported 4 problems")
    with pytest.raises(AssembleError, match="4 check problems, but 3"):
        parse_check_problems(log)


def test_a_log_without_a_finished_check_pass_raises() -> None:
    with pytest.raises(AssembleError, match="no CHECK pass"):
        parse_check_problems("3. Executing PROC pass.\n")
    unfinished = CHECK_LOG.split("Found and reported")[0]
    with pytest.raises(AssembleError, match="did not finish"):
        parse_check_problems(unfinished)


# --- IJTAG instrument specs --------------------------------------------------


def test_control_ports_are_write_tdrs_and_status_ports_read_tdrs() -> None:
    skip_unless_warptap()
    from warptap.icl_model import (  # type: ignore[import-not-found]
        InstrumentDirection,
        SignalBinding,
    )

    from faultflow.integrations.autombist_jtag import instrument_specs

    specs = instrument_specs(
        [
            SimpleNamespace(name="core0_ram_test_mode", role="control", width=1),
            SimpleNamespace(name="core0_ram_bist_fail", role="status", width=2),
        ]
    )
    assert [s.direction for s in specs] == [
        InstrumentDirection.WRITE,
        InstrumentDirection.READ,
    ]
    assert specs[1].signal_bits == (
        SignalBinding("core0_ram_bist_fail", 0),
        SignalBinding("core0_ram_bist_fail", 1),
    )


def test_an_unknown_instrument_role_is_refused() -> None:
    skip_unless_warptap()
    from faultflow.integrations.autombist_jtag import (
        AutombistJtagError,
        instrument_specs,
    )

    with pytest.raises(AutombistJtagError, match="role"):
        instrument_specs([SimpleNamespace(name="x", role="clock", width=1)])
