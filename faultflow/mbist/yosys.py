"""Reading the user's design with Yosys: elaborated, hierarchy kept, nothing
optimized -- what list-memories and mbist-insert see."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultflow.integrations.autombist import (
    CheckProblem,
    _quote,
    _run_yosys,
    parse_check_problems,
)
from faultflow.mbist.spec import DesignSources


@dataclass(frozen=True)
class Elaboration:
    top: str
    netlist: dict[str, Any]  # Yosys write_json
    json_path: Path
    log_path: Path
    # What Yosys `check` found in the design as written: problems the user's
    # design already has, which nothing inserted later is to blame for.
    problems: tuple[CheckProblem, ...]


def _token(text: str) -> str:
    """One Yosys script token, quoted."""
    return _quote(Path(text)) if " " in text or '"' in text else text


def read_design_lines(design: DesignSources) -> list[str]:
    """The Yosys commands that read the design: macro stubs as blackboxes
    (`read_verilog -lib`), then the sources as SystemVerilog, both with the
    design's defines and include directories."""
    options = [f"-D{define}" for define in design.defines] + [
        _token(f"-I{directory}") for directory in design.include_dirs
    ]
    lines = []
    if design.libs:
        lines.append(
            " ".join(
                ["read_verilog", "-lib", *options, *(_quote(p) for p in design.libs)]
            )
        )
    lines.append(
        " ".join(
            ["read_verilog", "-sv", *options, *(_quote(p) for p in design.sources)]
        )
    )
    return lines


def elaborate(design: DesignSources, top: str, *, workdir: Path) -> Elaboration:
    """Elaborate `top` from `design`: `hierarchy -check`, `proc`, and `check`
    for the problems the design has as written. No flatten, opt or abc: every
    module, instance name and generate block stays as the user wrote it."""
    workdir.mkdir(parents=True, exist_ok=True)
    json_path = workdir / f"{top}_elaborated.json"
    log_path = workdir / "elaborate.log"
    _run_yosys(
        [
            *read_design_lines(design),
            f"hierarchy -check -top {_token(top)}",
            "proc",
            "check",
            f"write_json {_quote(json_path)}",
        ],
        log_path=log_path,
        script_path=workdir / "elaborate.ys",
        error_message=f"Yosys could not elaborate the design (top {top})",
    )
    return Elaboration(
        top=top,
        netlist=json.loads(json_path.read_text(encoding="utf-8")),
        json_path=json_path,
        log_path=log_path,
        problems=parse_check_problems(log_path.read_text(encoding="utf-8")),
    )
