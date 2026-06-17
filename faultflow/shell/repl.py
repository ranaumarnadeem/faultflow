from __future__ import annotations

import logging
import sys
import tkinter
from configparser import ConfigParser
from pathlib import Path

from faultflow.config import ConfigError, load_config
from faultflow.project.profiles import profile_for_cell_map
from faultflow.shell.external import ExternalCommandResult, run_external
from faultflow.shell.formatting import format_error, format_prompt, format_result
from faultflow.shell.session import ProjectSession
from faultflow.shell.tcl_bridge import TclBridge


def _session_from_config(config_path: Path | None, output_root: Path) -> ProjectSession:
    if config_path is None:
        return ProjectSession(output_root=output_root)
    parser = ConfigParser()
    if not parser.read(config_path):
        raise ConfigError(f"Cannot read config: {config_path}")
    top = parser.get("design", "top", fallback="").strip()
    if not top:
        raise ConfigError("shell config requires [design] top")
    cfg = load_config(config_path, top)
    profile = profile_for_cell_map(cfg.cell_lib)
    session = ProjectSession(
        output_root=output_root,
        baseline_config=cfg,
    )
    session.read_netlist(cfg.netlist, top)
    session.use_lib_cells(profile.name)
    return session


def _write_command_output(result: ExternalCommandResult) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )
    if result.returncode:
        print(
            f"Command exited with status {result.returncode}.",
            file=sys.stderr,
        )


def run_shell(
    *,
    script: Path | None = None,
    config: Path | None = None,
    output_root: Path = Path("output"),
    verbose: bool = False,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    session = _session_from_config(config, output_root)
    bridge = TclBridge(session)
    if script is not None:
        if not script.exists():
            raise ConfigError(f"Cannot read Tcl script: {script}")
        try:
            bridge.eval(f"source {{{script}}}")
        except tkinter.TclError as exc:
            raise ConfigError(str(exc)) from exc
        return 0

    pending = ""
    while True:
        try:
            line = input(format_prompt(session) if not pending else "... ")
        except EOFError:
            print()
            return 0
        pending = f"{pending}\n{line}" if pending else line
        if not bool(int(bridge.interp.call("info", "complete", pending))):
            continue
        if pending.strip() in {"quit", "exit"}:
            return 0
        try:
            result = bridge.eval(pending)
            if str(result):
                decoded = bridge.decode_result(result)
                print(
                    format_result(decoded, session, verbose=verbose)
                    if decoded is not None
                    else result
                )
        except tkinter.TclError as exc:
            error_code = bridge.last_error_code()
            external = None
            if error_code[:3] == ("TCL", "LOOKUP", "COMMAND"):
                try:
                    words = tuple(
                        str(item) for item in bridge.interp.splitlist(pending)
                    )
                except tkinter.TclError:
                    words = ()
                external = run_external(words)
            if external is not None:
                _write_command_output(external)
            else:
                print(
                    format_error(str(exc), error_code),
                    file=sys.stderr,
                )
        pending = ""
