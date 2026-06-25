from __future__ import annotations

import fnmatch
import io
import shutil
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class CommandHelp:
    category: str
    usage: str
    summary: str
    details: str = ""
    requires: str = ""
    example: str = ""


COMMAND_HELP = {
    "read_netlist": CommandHelp(
        "Project",
        "read_netlist PATH -top MODULE",
        "Load Verilog or Yosys JSON",
        "Verilog is synthesized later with synth. Yosys JSON is already synthesized.",
        "No design may already be loaded.",
        "read_netlist examples/serial_adder.v -top serial_adder",
    ),
    "load_json": CommandHelp(
        "Project",
        "load_json PATH -top MODULE",
        "Load a synthesized Yosys JSON",
        "Loads an already-synthesized gate-level Yosys JSON directly, skipping "
        "synth (rejects Verilog -- use read_netlist for that). After synth the "
        "synthesized JSON is loaded automatically, so this is mainly for resuming "
        "from a previously synthesized netlist.",
        "No design may already be loaded.",
        "load_json output/serial_adder/intermediate/serial_adder.json "
        "-top serial_adder",
    ),
    "use_lib_cells": CommandHelp(
        "Project",
        "use_lib_cells PROFILE",
        "Select sky130 or osu035",
        "Select the registered runtime cell semantics and technology files.",
        "A registered PDK profile.",
        "use_lib_cells sky130",
    ),
    "synth": CommandHelp(
        "Project",
        "synth",
        "Synthesize loaded Verilog",
        "Runs Yosys for Verilog. For Yosys JSON this validates and returns "
        "already_synthesized.",
        "A loaded design and selected PDK profile.",
        "synth",
    ),
    "add_clock": CommandHelp(
        "Project",
        "add_clock PORT [-off 0|1]",
        "Declare a clock domain",
        "Registers PORT as a clock. -off sets the inactive level (0=default for "
        "posedge/active-high, 1 for negedge). A second add_clock for the same port "
        "replaces the prior entry. Declared clocks take authority over the name "
        "heuristic for domain identification.",
        "A design session (read_netlist not required).",
        "add_clock clk_a\nadd_clock clk_b -off 1",
    ),
    "report_clocks": CommandHelp(
        "Project",
        "report_clocks",
        "List declared clock domains",
        "Returns port names and off-states for all clocks declared via add_clock.",
        "",
        "report_clocks",
    ),
    "add_blackbox": CommandHelp(
        "Project",
        "add_blackbox INSTANCE",
        "Blackbox an instance by name",
        "Models INSTANCE as a test boundary: its input nets become observable "
        "(pseudo-PO) and its output nets become controllable (pseudo-PI), so the "
        "surrounding logic stays gradeable. The instance itself is not simulated. "
        "Repeated calls accumulate; a duplicate is ignored.",
        "A design session (read_netlist not required).",
        "add_blackbox u_sram\nadd_blackbox u_pll",
    ),
    "report_blackbox": CommandHelp(
        "Project",
        "report_blackbox",
        "List blackboxed instances",
        "Returns instance names declared via add_blackbox.",
        "",
        "report_blackbox",
    ),
    "check_cells": CommandHelp(
        "Project",
        "check_cells [-allow PATTERN]",
        "Audit netlist cell coverage vs the PDK cell map",
        "Compares every cell type in the synthesized netlist against the "
        "selected PDK JSON cell map (techmap) and reports the total cell count, "
        "the uncovered cell types (with counts) you must add to the techmap "
        "before they would be blackboxed or hard-fail, and any memory/macro-like "
        "types. Report-only: it never aborts the session. Repeat -allow to treat "
        "cell-type globs (e.g. '$scopeinfo') as intentional blackboxes.",
        "A synthesized design and a selected PDK profile.",
        "check_cells\ncheck_cells -allow $scopeinfo",
    ),
    "add_tp": CommandHelp(
        "Test Point",
        "add_tp [-m METRIC] [-t THRESHOLD] [-n MAX_POINTS]",
        "Insert test points via OpenTestability",
        "Calls OpenTestability's analyze_and_add_tp on the current netlist, "
        "runs ATPG on the resulting TPI netlist, and prints a Baseline|+TP|Δ "
        "comparison table. The TPI netlist becomes the new current version. "
        "Use reject_tp to revert. Requires [testpoint] opentest = /path/to/opentest "
        "in config and a completed sim campaign. "
        "-m: testability metric (default: scoap). "
        "-t: target threshold (default: 50). -n: max TPs to insert (default: 10).",
        "sim command must have been run first.",
        "add_tp\nadd_tp -m scoap -t 60 -n 5",
    ),
    "reject_tp": CommandHelp(
        "Test Point",
        "reject_tp",
        "Revert last test-point iteration",
        "Pops the current TP iteration from the version stack and restores the "
        "previous netlist as the active source. The TPI netlist files are preserved "
        "on disk but the active session returns to the prior iteration. "
        "Cannot revert past the original baseline.",
        "add_tp must have been run at least once.",
        "reject_tp",
    ),
    "wrap": CommandHelp(
        "Test Mode",
        "wrap [-model scan|buffer] [-clock PORT] [-se PORT] [-si PORT] [-so PORT] "
        "[-o PATH]",
        "Inject IEEE 1500 WBR cells on the boundary ports",
        "Adds wrapper boundary register (WBR) cells to every non-clock port of the "
        "current synthesized netlist. 'scan' model (default) emits native shiftable "
        "$wbc_*_scan_faultflow cells stitched into a dedicated wrapper chain with "
        "ports wbr_si/wbr_so/wbr_se; 'buffer' model emits transparent "
        "$wbc_*_faultflow cells (no extra scan ports). -clock names the port to "
        "leave unwrapped (shared clock; default: clk). The wrapped netlist is "
        "saved alongside the source and becomes the new active design; scan "
        "insertion state is cleared. After wrapping, use set_testmode intest or "
        "extest before add_scan / run_atpg.",
        "A synthesized Yosys JSON netlist.",
        "wrap -model scan -clock clk\n"
        "wrap -model scan -clock clk -se wbr_se -o /tmp/core_wrapped.json",
    ),
    "set_testmode": CommandHelp(
        "Test Mode",
        "set_testmode functional|intest|extest",
        "Select the IEEE 1500 wrapper test mode",
        "Sets the wrapper boundary test mode for a wrapped core. FUNCTIONAL "
        "leaves wrapper cells transparent. INTEST tests the core internals "
        "(wrapper input cells drive core inputs, output cells observe core "
        "outputs). EXTEST tests the interconnect around the core (the same "
        "cells flip to drive/observe the system side). INTEST and EXTEST are "
        "distinct runs and invalidate a resume vs FUNCTIONAL.",
        "A design session (read_netlist not required).",
        "set_testmode intest",
    ),
    "report_testmode": CommandHelp(
        "Test Mode",
        "report_testmode",
        "Show the current wrapper test mode",
        "Returns the active test mode (functional, intest, or extest).",
        "",
        "report_testmode",
    ),
    "add_scan": CommandHelp(
        "Scan",
        "add_scan -chains N [-max_length N] [-SI NAME] [-SO NAME] "
        "[-SE NAME] [-dry_run]",
        "Insert generic scan chains",
        "Inserts and stitches generic $scanff_faultflow cells. This command "
        "does not run scan checking or techmap.",
        "A synthesized design and selected PDK profile.",
        "add_scan -chains 4 -SI scan_in -SO scan_out -SE scan_en",
    ),
    "check_scan": CommandHelp(
        "Scan",
        "check_scan",
        "Validate current scan insertion",
        "Runs structural and normal-mode checks and records the result.",
        "A current generic scan insertion.",
        "check_scan",
    ),
    "run_atpg": CommandHelp(
        "Run",
        "run_atpg [-sa] [-scan] [-max ROUNDS] [-target PERCENT]",
        "Run native stuck-at ATPG",
        "Runs combinational ATPG by default. Use -scan for scan-protocol ATPG.",
        "A synthesized design; scan ATPG additionally requires a fresh scan check.",
        "run_atpg -sa -scan -max 20 -target 95",
    ),
    "status": CommandHelp(
        "Run",
        "status [-scan]",
        "Show campaign status",
        "Returns coverage, classification, terminal reason, and timing fields.",
        "A loaded project.",
        "status -scan",
    ),
    "report": CommandHelp(
        "Run",
        "report",
        "Regenerate unified report",
        "Writes report.rpt with scan headline and optional combinational comparison.",
        "A loaded project.",
        "report",
    ),
    "write_netlist": CommandHelp(
        "Output",
        "write_netlist [-scan] [-techmap|-notech] [-o PATH] [-verify]",
        "Publish a functional or scanned netlist",
        "-scan writes generic scan Verilog. -techmap binds supported physical "
        "scan cells. -verify is currently unsupported.",
        "A synthesized design; scanned writes require add_scan.",
        "write_netlist -scan -techmap",
    ),
    "write_patterns": CommandHelp(
        "Output",
        "write_patterns",
        "Export ATPG patterns",
        "Reserved for future scan-aware STIL/WGL export and currently unsupported.",
    ),
    "WORKERS": CommandHelp(
        "Options",
        "WORKERS ?N?",
        "Get or set the number of parallel SAT-ATPG worker processes",
        "With N: set atpg.workers to N (positive integer) and update the "
        "$WORKERS Tcl global. Without N: print the current value. The default "
        "is 1 (serial). Set to the number of available CPU cores for maximum "
        "throughput; each worker runs one C++ solver call, so wall-clock time "
        "scales roughly as 1/N. Falls back to serial with a warning on "
        "platforms that do not support fork (non-WSL Windows).",
        "",
        "WORKERS 10",
    ),
    "set_option": CommandHelp(
        "Options",
        "set_option KEY VALUE",
        "Set a persistent flow option",
        "Keys: atpg.max_rounds, atpg.sat_timeout_seconds,"
        " atpg.sat_timeout_schedule, atpg.workers, report.threshold,"
        " simulation.unsupported_cells. atpg.sat_timeout_schedule is a"
        " comma list like 2,10,60 that escalates a fault's SAT timeout only"
        " when it times out. atpg.workers sets parallel workers (1=serial);"
        " prefer the WORKERS shorthand.",
        "",
        "set_option atpg.sat_timeout_schedule 2,10,60",
    ),
    "unset_option": CommandHelp(
        "Options",
        "unset_option KEY",
        "Remove a persistent option override",
    ),
    "show_config": CommandHelp(
        "Options",
        "show_config",
        "Show current option overrides",
    ),
    "save_session": CommandHelp(
        "Session",
        "save_session",
        "Validate and checkpoint the session",
    ),
    "load_session": CommandHelp(
        "Session",
        "load_session TOP",
        "Load saved project definition",
        "Restores project identity and options without trusting derived run state.",
    ),
    "resume": CommandHelp(
        "Session",
        "resume TOP",
        "Resume validated project state",
        "Reloads available synthesis, scan, check, and campaign state.",
    ),
    "clean": CommandHelp(
        "Session",
        "clean",
        "Remove campaign database state",
        "Preserves manifests, session, logs, netlists, and other project artifacts.",
    ),
    "reset": CommandHelp(
        "Session",
        "reset",
        "Clear the in-memory project",
    ),
    "help": CommandHelp(
        "Shell",
        "help [COMMAND]",
        "Show command help",
    ),
    "quit": CommandHelp(
        "Shell",
        "quit",
        "Exit the interactive shell",
    ),
    "exit": CommandHelp(
        "Shell",
        "exit",
        "Exit the interactive shell",
    ),
}


CATEGORY_ORDER = (
    "Project",
    "Test Mode",
    "Test Point",
    "Scan",
    "Run",
    "Output",
    "Options",
    "Session",
    "Shell",
)


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _make_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    tty = _is_tty()
    try:
        width = shutil.get_terminal_size().columns if tty else 9999
    except Exception:
        width = 88
    return Console(file=buf, highlight=False, force_terminal=tty, width=width), buf


def _usage_text(usage: str) -> Text:
    """Bold command name, dim flags/args."""
    parts = usage.split(" ", 1)
    t = Text(parts[0], style="bold")
    if len(parts) > 1:
        t.append(" " + parts[1], style="dim")
    return t


def render_help_overview() -> str:
    console, buf = _make_console()

    console.print(Text("faultflow", style="bold"))
    console.print()

    for category in CATEGORY_ORDER:
        entries = [item for item in COMMAND_HELP.values() if item.category == category]
        if not entries:
            continue

        console.print(Text("  " + category, style="bold cyan"))

        tbl = Table(box=None, show_header=False, padding=(0, 2, 0, 4), show_edge=False)
        tbl.add_column(no_wrap=True)
        tbl.add_column()
        for item in entries:
            tbl.add_row(_usage_text(item.usage), Text(item.summary, style="dim"))
        console.print(tbl)
        console.print()

    # The exact literal substring must survive for the test assertion.
    footer = Text('  Use "', style="dim")
    footer.append("help <command>", style="bold")
    footer.append('" for details.', style="dim")
    console.print(footer)
    return buf.getvalue()


def render_command_matches(pattern: str) -> str | None:
    """Render a flat table of commands whose names match a glob pattern.

    Returns None when nothing matches so the caller can raise. Used by
    ``help <glob>`` (e.g. ``help re*`` lists every command starting with re).
    """
    names = sorted(fnmatch.filter(COMMAND_HELP.keys(), pattern))
    if not names:
        return None
    console, buf = _make_console()

    console.print(Text(f'commands matching "{pattern}"', style="bold"))
    console.print()

    tbl = Table(box=None, show_header=False, padding=(0, 2, 0, 4), show_edge=False)
    tbl.add_column(no_wrap=True)
    tbl.add_column()
    for name in names:
        item = COMMAND_HELP[name]
        tbl.add_row(_usage_text(item.usage), Text(item.summary, style="dim"))
    console.print(tbl)
    return buf.getvalue()


def render_command_help(command: str) -> str:
    item = COMMAND_HELP[command]
    console, buf = _make_console()

    # Header — must start with bare command name (test: text.startswith("add_scan"))
    header = Text(command, style="bold bright_white")
    header.append("  ")
    header.append(item.summary, style="dim")
    console.print(header)
    console.print()

    # Usage: prefix dim, args bold — use Text to avoid markup parsing of [] in usage
    usage_line = Text()
    usage_line.append("Usage: ", style="dim")
    usage_line.append(item.usage, style="bold")
    console.print(usage_line)

    if item.details:
        console.print()
        console.print(Text("  " + item.details))

    if item.requires:
        console.print()
        console.print(Text("Requires:", style="dim"))
        console.print(Text("  " + item.requires))

    if item.example:
        console.print()
        console.print(Text("Example:", style="dim"))
        for line in item.example.split("\n"):
            t = Text("  ")
            t.append(line, style="bold green")
            console.print(t)

    return buf.getvalue()
