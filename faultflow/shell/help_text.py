from __future__ import annotations

from dataclasses import dataclass


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
    "set_option": CommandHelp(
        "Options",
        "set_option KEY VALUE",
        "Set a persistent flow option",
        "Supported keys are shown by show_config.",
        "",
        "set_option atpg.max_rounds 40",
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


def render_help_overview() -> str:
    width = max(len(item.usage) for item in COMMAND_HELP.values())
    lines = ["Faultflow commands", ""]
    for category in CATEGORY_ORDER:
        entries = [item for item in COMMAND_HELP.values() if item.category == category]
        if not entries:
            continue
        lines.append(category)
        for item in entries:
            lines.append(f"  {item.usage:<{width}}  {item.summary}")
        lines.append("")
    lines.append('Use "help <command>" for details.')
    return "\n".join(lines)


def render_command_help(command: str) -> str:
    item = COMMAND_HELP[command]
    lines = [command, "", f"Usage: {item.usage}", "", item.summary + "."]
    if item.details:
        lines.extend(["", item.details])
    if item.requires:
        lines.extend(["", "Requires:", f"  {item.requires}"])
    if item.example:
        lines.extend(["", "Example:", f"  {item.example}"])
    return "\n".join(lines)
