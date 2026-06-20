from __future__ import annotations

import tkinter
from pathlib import Path
from typing import Any, Callable

from faultflow.shell.errors import ShellError, unsupported
from faultflow.shell.help_text import (
    COMMAND_HELP,
    render_command_help,
    render_help_overview,
)
from faultflow.shell.logging_setup import log_exception
from faultflow.shell.session import ProjectSession


class TclBridge:
    def __init__(self, session: ProjectSession) -> None:
        self.session = session
        self.interp = tkinter.Tcl()
        self._handlers: dict[str, Callable[[list[str]], Any]] = {
            "read_netlist": self._read_netlist,
            "use_lib_cells": self._use_lib_cells,
            "synth": self._synth,
            "add_scan": self._add_scan,
            "check_scan": self._check_scan,
            "run_atpg": self._run_atpg,
            "status": self._status,
            "report": self._report,
            "write_netlist": self._write_netlist,
            "write_patterns": self._write_patterns,
            "set_option": self._set_option,
            "unset_option": self._unset_option,
            "show_config": self._show_config,
            "save_session": self._save_session,
            "load_session": self._load_session,
            "resume": self._resume,
            "clean": self._clean,
            "reset": self._reset,
            "add_clock": self._add_clock,
            "report_clocks": self._report_clocks,
            "add_blackbox": self._add_blackbox,
            "report_blackbox": self._report_blackbox,
            "add_tp": self._add_tp,
            "reject_tp": self._reject_tp,
            "set_testmode": self._set_testmode,
            "report_testmode": self._report_testmode,
            "check_cells": self._check_cells,
            "help": self._help,
            "quit": self._quit,
            "exit": self._quit,
        }
        for name in self._handlers:
            hidden = f"__faultflow_{name}"
            self.interp.createcommand(hidden, self._callback(name))
            self.interp.eval(f"""
proc {name} {{args}} {{
    set envelope [{hidden} {{*}}$args]
    if {{[dict get $envelope status] eq "error"}} {{
        return -code error \
            -errorcode [dict get $envelope errorcode] \
            [dict get $envelope message]
    }}
    return [dict get $envelope result]
}}
""")

    def _dict(self, values: dict[str, Any]) -> Any:
        args: list[Any] = []
        for key, value in values.items():
            args.extend((key, self._value(value)))
        return self.interp.call("dict", "create", *args)

    def _value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._dict(value)
        if isinstance(value, (list, tuple)):
            return tuple(self._value(item) for item in value)
        if isinstance(value, Path):
            return str(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return int(value)
        return value

    def _callback(self, name: str) -> Callable[..., Any]:
        def invoke(*args: str) -> Any:
            try:
                result = self._handlers[name](list(args))
                if hasattr(result, "operation"):
                    payload = {
                        "status": "ok",
                        "command": result.operation,
                        "top": result.top,
                        "message": result.message,
                        "artifacts": dict(result.artifacts),
                        "metrics": dict(result.metrics),
                        "warnings": list(result.warnings),
                    }
                    payload.update(dict(result.metrics))
                else:
                    payload = {"status": "ok", "command": name, "message": result}
                return self._dict({"status": "ok", "result": self._dict(payload)})
            except ShellError as exc:
                log_exception(self.session.output_root, self.session.top)
                return self._dict(
                    {
                        "status": "error",
                        "message": str(exc),
                        "errorcode": exc.code,
                    }
                )
            except (ValueError, TypeError) as exc:
                log_exception(self.session.output_root, self.session.top)
                return self._dict(
                    {
                        "status": "error",
                        "message": str(exc),
                        "errorcode": (
                            "FAULTFLOW",
                            "CONFIG",
                            "INVALID_VALUE",
                        ),
                    }
                )
            except Exception as exc:
                log_exception(self.session.output_root, self.session.top)
                return self._dict(
                    {
                        "status": "error",
                        "message": str(exc),
                        "errorcode": ("FAULTFLOW", "INTERNAL", "UNEXPECTED"),
                    }
                )

        return invoke

    def eval(self, script: str) -> Any:
        return self.interp.eval(script)

    def decode_result(self, value: Any) -> dict[str, object] | None:
        try:
            keys = self.interp.splitlist(self.interp.call("dict", "keys", value))
        except tkinter.TclError:
            return None
        if not {"status", "command", "message"}.issubset(keys):
            return None
        decoded: dict[str, object] = {}
        for key in keys:
            raw = self.interp.call("dict", "get", value, key)
            if key in {"artifacts", "metrics"}:
                decoded[key] = self._decode_nested_dict(raw)
            elif key == "warnings":
                decoded[key] = tuple(self.interp.splitlist(raw))
            else:
                decoded[key] = raw
        return decoded

    def last_error_code(self) -> tuple[str, ...]:
        try:
            value = self.interp.getvar("errorCode")
            return tuple(str(item) for item in self.interp.splitlist(value))
        except tkinter.TclError:
            return ()

    def _decode_nested_dict(self, value: Any) -> dict[str, object]:
        try:
            keys = self.interp.splitlist(self.interp.call("dict", "keys", value))
        except tkinter.TclError:
            return {}
        return {str(key): self.interp.call("dict", "get", value, key) for key in keys}

    def call(self, command: str, *args: str) -> Any:
        if command not in self._handlers:
            raise ShellError(f"unknown command: {command}", "INPUT", "INVALID_OPTION")
        return self._handlers[command](list(args))

    def _read_netlist(self, args: list[str]) -> Any:
        if len(args) != 3 or args[1] != "-top":
            raise ShellError(
                "usage: read_netlist PATH -top MODULE",
                "CONFIG",
                "INVALID_OPTION",
            )
        return self.session.read_netlist(Path(args[0]), args[2])

    def _use_lib_cells(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError("usage: use_lib_cells PROFILE", "CONFIG", "INVALID_OPTION")
        return self.session.use_lib_cells(args[0])

    def _synth(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: synth", "CONFIG", "INVALID_OPTION")
        return self.session.synthesize()

    def _add_scan(self, args: list[str]) -> Any:
        options: dict[str, object] = {}
        index = 0
        while index < len(args):
            key = args[index]
            if key == "-dry_run":
                options["dry_run"] = True
                index += 1
                continue
            mapping = {
                "-chains": "scan_chains",
                "-max_length": "max_chain_length",
                "-SI": "scan_in",
                "-SO": "scan_out",
                "-SE": "scan_enable",
            }
            if key not in mapping or index + 1 >= len(args):
                raise ShellError(
                    f"invalid add_scan option: {key}",
                    "CONFIG",
                    "INVALID_OPTION",
                )
            value: object = args[index + 1]
            if key in {"-chains", "-max_length"}:
                value = int(str(value))
            options[mapping[key]] = value
            index += 2
        if "scan_chains" not in options:
            raise ShellError("add_scan requires -chains N", "CONFIG", "INVALID_OPTION")
        return self.session.add_scan(**options)

    def _run_atpg(self, args: list[str]) -> Any:
        scan = False
        options: dict[str, object] = {}
        index = 0
        while index < len(args):
            key = args[index]
            if key == "-scan":
                scan = True
                index += 1
            elif key == "-serial_ref":
                options["serial_ref"] = True
                index += 1
            elif key == "-sa":
                index += 1
            elif key == "-tf":
                raise unsupported(
                    "transition-fault ATPG is not supported", "TRANSITION_FAULT"
                )
            elif key in {"-max", "-target"} and index + 1 < len(args):
                target = "max_rounds" if key == "-max" else "target_coverage"
                raw = args[index + 1]
                options[target] = int(raw) if key == "-max" else float(raw)
                index += 2
            else:
                raise ShellError(
                    f"invalid run_atpg option: {key}",
                    "CONFIG",
                    "INVALID_OPTION",
                )
        return self.session.run_atpg(scan=scan, **options)

    def _check_scan(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: check_scan", "CONFIG", "INVALID_OPTION")
        return self.session.check_scan()

    def _status(self, args: list[str]) -> Any:
        if args not in ([], ["-scan"]):
            raise ShellError("usage: status ?-scan?", "CONFIG", "INVALID_OPTION")
        return self.session.status(scan=args == ["-scan"])

    def _report(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: report", "CONFIG", "INVALID_OPTION")
        return self.session.report()

    def _write_netlist(self, args: list[str]) -> Any:
        scan = False
        techmap = False
        verify = False
        output: Path | None = None
        index = 0
        while index < len(args):
            key = args[index]
            if key == "-scan":
                scan = True
                index += 1
            elif key == "-techmap":
                techmap = True
                index += 1
            elif key == "-notech":
                techmap = False
                index += 1
            elif key == "-verify":
                verify = True
                index += 1
            elif key == "-o" and index + 1 < len(args):
                output = Path(args[index + 1])
                index += 2
            else:
                raise ShellError(
                    f"invalid write_netlist option: {key}",
                    "CONFIG",
                    "INVALID_OPTION",
                )
        return self.session.write_netlist(
            scan=scan, techmap=techmap, verify=verify, output=output
        )

    def _set_option(self, args: list[str]) -> Any:
        if len(args) != 2:
            raise ShellError("usage: set_option KEY VALUE", "CONFIG", "INVALID_OPTION")
        return self.session.set_option(args[0], args[1])

    def _unset_option(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError("usage: unset_option KEY", "CONFIG", "INVALID_OPTION")
        return self.session.unset_option(args[0])

    def _show_config(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: show_config", "CONFIG", "INVALID_OPTION")
        return {"options": dict(self.session.options)}

    def _save_session(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: save_session", "CONFIG", "INVALID_OPTION")
        return self.session.save_session()

    def _load_session(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError("usage: load_session TOP", "CONFIG", "INVALID_OPTION")
        return self.session.load_session(args[0])

    def _resume(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError("usage: resume TOP", "CONFIG", "INVALID_OPTION")
        return self.session.load_session(args[0], resume=True)

    def _clean(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: clean", "CONFIG", "INVALID_OPTION")
        return self.session.clean()

    def _reset(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: reset", "CONFIG", "INVALID_OPTION")
        self.session.reset()
        return "session reset"

    def _add_clock(self, args: list[str]) -> Any:
        if not args:
            raise ShellError(
                "usage: add_clock PORT [-off 0|1]", "CONFIG", "INVALID_OPTION"
            )
        port = args[0]
        off_state = 0
        idx = 1
        while idx < len(args):
            flag = args[idx]
            if flag == "-off":
                if idx + 1 >= len(args):
                    raise ShellError(
                        "add_clock: -off requires a value", "CONFIG", "INVALID_OPTION"
                    )
                val = args[idx + 1]
                if val not in ("0", "1"):
                    raise ShellError(
                        f"add_clock: -off must be 0 or 1, got {val!r}",
                        "CONFIG",
                        "INVALID_VALUE",
                    )
                off_state = int(val)
                idx += 2
            else:
                raise ShellError(
                    f"add_clock: unknown option {flag!r}", "CONFIG", "INVALID_OPTION"
                )
        self.session.add_clock(port, off_state=off_state)
        return f"clock domain: {port} off_state={off_state}"

    def _report_clocks(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: report_clocks", "CONFIG", "INVALID_OPTION")
        return {"clocks": self.session.report_clocks()}

    def _add_blackbox(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError("usage: add_blackbox INSTANCE", "CONFIG", "INVALID_OPTION")
        self.session.add_blackbox(args[0])
        return f"blackbox instance: {args[0]}"

    def _report_blackbox(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: report_blackbox", "CONFIG", "INVALID_OPTION")
        return {"blackbox": self.session.report_blackbox()}

    def _add_tp(self, args: list[str]) -> Any:
        kwargs: dict[str, object] = {}
        idx = 0
        while idx < len(args):
            flag = args[idx]
            if flag in ("-m", "--metric"):
                if idx + 1 >= len(args):
                    raise ShellError(
                        f"add_tp: {flag} requires a value",
                        "CONFIG",
                        "INVALID_OPTION",
                    )
                kwargs["metric"] = args[idx + 1]
                idx += 2
            elif flag in ("-t", "--threshold"):
                if idx + 1 >= len(args):
                    raise ShellError(
                        f"add_tp: {flag} requires a value",
                        "CONFIG",
                        "INVALID_OPTION",
                    )
                try:
                    kwargs["threshold"] = int(args[idx + 1])
                except ValueError:
                    raise ShellError(
                        f"add_tp: threshold must be integer, got {args[idx+1]!r}",
                        "CONFIG",
                        "INVALID_VALUE",
                    )
                idx += 2
            elif flag in ("-n", "--max-points"):
                if idx + 1 >= len(args):
                    raise ShellError(
                        f"add_tp: {flag} requires a value",
                        "CONFIG",
                        "INVALID_OPTION",
                    )
                try:
                    kwargs["max_points"] = int(args[idx + 1])
                except ValueError:
                    raise ShellError(
                        f"add_tp: max_points must be integer, got {args[idx+1]!r}",
                        "CONFIG",
                        "INVALID_VALUE",
                    )
                idx += 2
            else:
                raise ShellError(
                    f"add_tp: unknown option {flag!r}", "CONFIG", "INVALID_OPTION"
                )
        comparison = self.session.add_tp(**kwargs)  # type: ignore[arg-type]
        return comparison

    def _reject_tp(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: reject_tp", "CONFIG", "INVALID_OPTION")
        return self.session.reject_tp()

    def _set_testmode(self, args: list[str]) -> Any:
        if len(args) != 1:
            raise ShellError(
                "usage: set_testmode functional|intest|extest",
                "CONFIG",
                "INVALID_OPTION",
            )
        self.session.set_testmode(args[0])
        return f"test mode: {self.session.report_testmode()}"

    def _report_testmode(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: report_testmode", "CONFIG", "INVALID_OPTION")
        return {"test_mode": self.session.report_testmode()}

    def _check_cells(self, args: list[str]) -> Any:
        allow: list[str] = []
        idx = 0
        while idx < len(args):
            flag = args[idx]
            if flag == "-allow":
                if idx + 1 >= len(args):
                    raise ShellError(
                        "check_cells: -allow requires a PATTERN",
                        "CONFIG",
                        "INVALID_OPTION",
                    )
                allow.append(args[idx + 1])
                idx += 2
            else:
                raise ShellError(
                    f"check_cells: unknown option {flag!r}",
                    "CONFIG",
                    "INVALID_OPTION",
                )
        return self.session.check_cells(allow=allow)

    def _help(self, args: list[str]) -> Any:
        if len(args) > 1:
            raise ShellError("usage: help ?COMMAND?", "CONFIG", "INVALID_OPTION")
        if args:
            if args[0] not in COMMAND_HELP:
                raise ShellError(
                    f"unknown command: {args[0]}", "CONFIG", "INVALID_OPTION"
                )
            return render_command_help(args[0])
        return render_help_overview()

    def _quit(self, args: list[str]) -> Any:
        if args:
            raise ShellError("usage: quit", "CONFIG", "INVALID_OPTION")
        return "quit"

    def _write_patterns(self, args: list[str]) -> Any:
        del args
        raise unsupported(
            "STIL/WGL pattern export is not implemented", "PATTERN_EXPORT"
        )
