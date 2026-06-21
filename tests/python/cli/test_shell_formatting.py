from __future__ import annotations

from pathlib import Path

from faultflow.shell.formatting import (
    format_error,
    format_prompt,
    format_result,
)
from faultflow.shell.repl import run_shell
from faultflow.shell.session import ProjectSession


def test_use_lib_cells_result_is_human_readable() -> None:
    text = format_result(
        {
            "status": "ok",
            "command": "use_lib_cells",
            "top": "",
            "message": "using technology profile sky130",
            "artifacts": {},
            "metrics": {},
            "warnings": (),
        },
        ProjectSession(),
    )

    assert text == (
        "Technology profile: sky130\n" "Next: read_netlist <path> -top <module>"
    )
    assert "status ok" not in text
    assert "artifacts" not in text


def test_show_config_result_includes_project_state_and_option_overrides() -> None:
    session = ProjectSession()
    session.profile = object_with_name("sky130")
    session.options["atpg.max_rounds"] = "40"

    text = format_result(
        {
            "status": "ok",
            "command": "show_config",
            "message": {"options": {"atpg.max_rounds": "40"}},
        },
        session,
    )

    assert "Project" in text
    assert "Design:       not loaded" in text
    assert "Technology:   sky130" in text
    assert "Option overrides" in text
    assert "atpg.max_rounds = 40" in text


def test_run_atpg_result_highlights_coverage_and_timing() -> None:
    session = ProjectSession()
    session.top = "c17"
    text = format_result(
        {
            "status": "ok",
            "command": "run_atpg",
            "top": "c17",
            "message": "technical runner output",
            "metrics": {
                "detected": 20,
                "effective_denominator": 22,
                "test_coverage_percent": 90.909,
                "fault_coverage_percent": 95.0,
                "terminal_reason": "THRESHOLD_MET",
                "vectors": 7,
                "atpg_seconds": 1.25,
                "simulation_seconds": 2.5,
            },
            "artifacts": {},
            "warnings": (),
        },
        session,
    )

    assert "ATPG complete: c17" in text
    assert "Test coverage:  90.909% (20/22 detected)" in text
    assert "Fault coverage: 95.000%" in text
    assert "Terminal:       THRESHOLD_MET" in text
    assert "Timing:         ATPG 1.250s, simulation 2.500s" in text
    assert "technical runner output" not in text


def test_verbose_result_includes_raw_message_artifacts_and_metrics() -> None:
    session = ProjectSession()
    text = format_result(
        {
            "status": "ok",
            "command": "report",
            "top": "demo",
            "message": "report written",
            "metrics": {"campaign_state": "available"},
            "artifacts": {"report": "output/demo/report.rpt"},
            "warnings": ("legacy database ignored",),
        },
        session,
        verbose=True,
    )

    assert "report written" in text
    assert "Artifacts:" in text
    assert "report: output/demo/report.rpt" in text
    assert "Metrics:" in text
    assert "campaign_state: available" in text
    assert "Warnings:" in text


def test_precondition_error_explains_the_next_command() -> None:
    text = format_error(
        "run read_netlist first",
        ("FAULTFLOW", "PRECONDITION", "NO_DESIGN"),
    )

    assert text == ("No design is loaded.\n" "Next: read_netlist <path> -top <module>")


def test_usage_error_is_rendered_as_usage_not_internal_error() -> None:
    text = format_error(
        "usage: read_netlist PATH -top MODULE",
        ("FAULTFLOW", "CONFIG", "INVALID_OPTION"),
    )

    assert text == "Usage: read_netlist PATH -top MODULE"


def test_prompt_reflects_loaded_project_and_profile() -> None:
    session = ProjectSession()
    assert format_prompt(session) == "faultflow> "

    session.profile = object_with_name("sky130")
    assert format_prompt(session) == "faultflow[sky130]> "

    session.top = "demo"
    assert format_prompt(session) == "faultflow[demo:sky130]> "


def test_interactive_shell_formats_success_and_error_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    commands = iter(
        [
            "use_lib_cells sky130",
            "show_config",
            "synth",
            "quit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(commands))

    assert run_shell(output_root=tmp_path / "output") == 0

    captured = capsys.readouterr()
    assert "Technology profile: sky130" in captured.out
    assert "Design:       not loaded" in captured.out
    assert "status ok" not in captured.out
    assert "No design is loaded." in captured.err
    assert "Next: read_netlist <path> -top <module>" in captured.err


def object_with_name(name: str):
    return type("ProfileStub", (), {"name": name})()
