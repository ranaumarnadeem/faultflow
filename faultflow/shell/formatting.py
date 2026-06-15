from __future__ import annotations

from collections.abc import Mapping, Sequence

from faultflow.shell.session import ProjectSession


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[tuple[str, object]]:
    return sorted((str(key), item) for key, item in _mapping(value).items())


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _profile_name(session: ProjectSession) -> str:
    return session.profile.name if session.profile is not None else "not selected"


def _project_state(session: ProjectSession) -> list[str]:
    return [
        "Project",
        f"  Design:       {session.top or 'not loaded'}",
        f"  Source:       {session.source or 'not loaded'}",
        f"  Technology:   {_profile_name(session)}",
        f"  Synthesized:  {'yes' if session.synthesized else 'no'}",
        f"  Scan inserted:{' yes' if session.scan_inserted else ' no'}",
        f"  Scan checked: {'yes' if session.scan_checked else 'no'}",
    ]


def _format_atpg(result: Mapping[str, object]) -> str:
    metrics = _mapping(result.get("metrics"))
    top = str(result.get("top") or "design")
    state = str(metrics.get("campaign_state") or "")
    if state == "not_run":
        return f"No ATPG campaign has been run for {top}."
    detected = _integer(metrics.get("detected"))
    denominator = _integer(metrics.get("effective_denominator"))
    test_coverage = _number(metrics.get("test_coverage_percent"))
    fault_coverage = _number(metrics.get("fault_coverage_percent"))
    terminal = str(metrics.get("terminal_reason") or "n/a")
    vectors = _integer(metrics.get("vectors"))
    atpg_seconds = _number(metrics.get("atpg_seconds"))
    simulation_seconds = _number(metrics.get("simulation_seconds"))
    return "\n".join(
        [
            f"ATPG complete: {top}",
            f"  Test coverage:  {test_coverage:.3f}% "
            f"({detected}/{denominator} detected)",
            f"  Fault coverage: {fault_coverage:.3f}%",
            f"  Terminal:       {terminal}",
            f"  Patterns:       {vectors}",
            f"  Timing:         ATPG {atpg_seconds:.3f}s, "
            f"simulation {simulation_seconds:.3f}s",
        ]
    )


def _format_config(session: ProjectSession) -> str:
    lines = _project_state(session)
    lines.extend(["", "Option overrides"])
    if session.options:
        lines.extend(
            f"  {key} = {value}" for key, value in sorted(session.options.items())
        )
    else:
        lines.append("  none")
    return "\n".join(lines)


def _format_primary(result: Mapping[str, object], session: ProjectSession) -> str:
    command = str(result.get("command") or "")
    message = result.get("message")
    if command == "help":
        return str(message or "")
    if command == "show_config":
        return _format_config(session)
    if command == "use_lib_cells":
        profile = _profile_name(session)
        if profile == "not selected":
            prefix = "using technology profile "
            text = str(message or "")
            if text.startswith(prefix):
                profile = text[len(prefix) :]
        lines = [f"Technology profile: {profile}"]
        if session.top is None:
            lines.append("Next: read_netlist <path> -top <module>")
        elif not session.synthesized:
            lines.append("Next: synth")
        return "\n".join(lines)
    if command == "read_netlist":
        lines = [
            f"Design loaded: {session.top or result.get('top') or 'unknown'}",
            f"Source: {session.source or message or 'unknown'}",
        ]
        lines.append(
            "Next: use_lib_cells <sky130|osu035>"
            if session.profile is None
            else "Next: synth"
        )
        return "\n".join(lines)
    if command == "synth":
        if "already synthesized" in str(message):
            return f"Netlist ready: {session.top or result.get('top') or 'design'}"
        return f"Synthesis complete: {session.top or result.get('top') or 'design'}"
    if command in {"run_atpg", "status"}:
        return _format_atpg(result)
    if command == "report":
        artifacts = _mapping(result.get("artifacts"))
        path = artifacts.get("report")
        return f"Report written: {path}" if path else str(message or "Report written.")
    if command in {"scan", "add_scan"}:
        return (
            f"Scan insertion complete: {result.get('top') or session.top or 'design'}"
        )
    if command in {"scan-check", "check_scan"}:
        return f"Scan check passed: {result.get('top') or session.top or 'design'}"
    if command == "write_netlist":
        artifacts = _mapping(result.get("artifacts"))
        path = artifacts.get("netlist") or artifacts.get("netlist_verilog")
        return (
            f"Netlist written: {path}" if path else str(message or "Netlist written.")
        )
    if command == "clean":
        return str(message or "Campaign state cleaned.")
    if command == "reset":
        return "Session reset."
    if command in {
        "set_option",
        "unset_option",
        "save_session",
        "load_session",
        "resume",
    }:
        return str(message or command.replace("_", " ").capitalize())
    return str(message or "")


def _format_details(result: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    message = str(result.get("message") or "")
    if message:
        lines.extend(["", "Details:", f"  {message}"])
    artifacts = _items(result.get("artifacts"))
    if artifacts:
        lines.extend(["", "Artifacts:"])
        lines.extend(f"  {key}: {value}" for key, value in artifacts)
    metrics = _items(result.get("metrics"))
    if metrics:
        lines.extend(["", "Metrics:"])
        lines.extend(f"  {key}: {value}" for key, value in metrics)
    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)):
        warning_items = [str(item) for item in warnings if str(item)]
        if warning_items:
            lines.extend(["", "Warnings:"])
            lines.extend(f"  {item}" for item in warning_items)
    return lines


def format_result(
    result: Mapping[str, object],
    session: ProjectSession,
    *,
    verbose: bool = False,
) -> str:
    primary = _format_primary(result, session)
    if not verbose:
        warnings = result.get("warnings")
        warning_items = (
            [str(item) for item in warnings if str(item)]
            if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes))
            else []
        )
        if warning_items:
            return "\n".join([primary, *(f"Warning: {item}" for item in warning_items)])
        return primary
    return "\n".join([primary, *_format_details(result)]).rstrip()


def format_error(message: str, code: Sequence[str]) -> str:
    if message.lower().startswith("usage:"):
        return f"Usage:{message.split(':', 1)[1]}"
    leaf = code[-1] if code else ""
    guidance = {
        "NO_DESIGN": (
            "No design is loaded.",
            "read_netlist <path> -top <module>",
        ),
        "PROFILE_REQUIRED": (
            "No technology profile is selected.",
            "use_lib_cells <sky130|osu035>",
        ),
        "SYNTH_REQUIRED": ("The design is not synthesized.", "synth"),
        "SCAN_REQUIRED": (
            "No scan insertion is available.",
            "add_scan -chains <count>",
        ),
        "SCAN_CHECK_REQUIRED": (
            "The current scan insertion has not been checked.",
            "check_scan",
        ),
        "SCAN_CHECK_STALE": (
            "The scan check is stale for the current netlist.",
            "check_scan",
        ),
        "CAMPAIGN_STALE": (
            "The campaign belongs to an older scan configuration.",
            "clean",
        ),
    }
    if leaf in guidance:
        summary, next_command = guidance[leaf]
        return f"{summary}\nNext: {next_command}"
    return message


def format_prompt(session: ProjectSession) -> str:
    profile = session.profile.name if session.profile is not None else None
    context = ":".join(part for part in (session.top, profile) if part)
    return f"faultflow[{context}]> " if context else "faultflow> "
