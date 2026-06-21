# =============================================================================
# flowscripts/core_atpg.tcl
#
# Per-core ATPG: combines scan insertion and INTEST into a single flow for one
# wrapped block.  The scan chains are stitched, the session is saved, and then
# INTEST scan ATPG is run in the same session — no reset between phases.
#
# This script also defines ff::run_core_atpg so that hereichy_atpg.tcl can
# call it in a loop for each block without re-sourcing the file.
#
# Usage (standalone — runs scan + INTEST for a single block):
#   python ff.py shell -f flowscripts/core_atpg.tcl
#
# Usage (sourced by hereichy_atpg.tcl):
#   set _ff_scan_sourced   1
#   set _ff_intest_sourced 1
#   set _ff_core_sourced   1
#   source {flowscripts/core_atpg.tcl}
#   # ff::run_core_atpg is now available
# =============================================================================

# Import scan and intest proc definitions without triggering their entry points
set ::_ff_scan_sourced   1
source [file join [file dirname [info script]] scan.tcl]

set ::_ff_intest_sourced 1
source [file join [file dirname [info script]] intest.tcl]

namespace eval ff {}
proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_core_atpg
#
# Runs the full per-block flow: scan insertion → save session → INTEST ATPG.
# The two phases share the same session object (no reset between them).
#
# Required arguments (key value):
#   top            — module name
#   netlist        — path to .v or .json
#   lib_cells      — PDK profile: sky130 | osu035
#   clock_port     — primary clock port name
#   scan_chains    — number of scan chains
#
# Optional arguments (key value):
#   scan_in        — scan-input port           [scan_in]
#   scan_out       — scan-output port          [scan_out]
#   scan_enable    — scan-enable port          [scan_en]
#   max_chain_len  — max FFs per chain; 0=∞   [0]
#   clock_off      — inactive clock level      [0]
#   atpg_target    — INTEST coverage target %  [95.0]
#   atpg_max_rounds — INTEST ATPG round cap   [20]
#   sat_timeout    — per-fault SAT timeout (s) [10]
#
# Returns:
#   dict with keys:
#     top           — the block name
#     scan_result   — result dict from add_scan (after insertion)
#     intest_result — status dict from [status -scan] after INTEST ATPG
# ---------------------------------------------------------------------------
proc ff::run_core_atpg {args} {
    array set opt {
        scan_in         "scan_in"
        scan_out        "scan_out"
        scan_enable     "scan_en"
        max_chain_len   0
        clock_off       0
        atpg_target     95.0
        atpg_max_rounds 20
        sat_timeout     10
    }
    array set opt $args
    foreach req {top netlist lib_cells clock_port scan_chains} {
        if {![info exists opt($req)]} {
            error "run_core_atpg: required argument '$req' not provided"
        }
    }

    ff::log "###################################################"
    ff::log "core ATPG: $opt(top)"
    ff::log "###################################################"

    # ---- Phase 1: Scan insertion ----------------------------------------
    ff::log "--- Phase 1: scan insertion ---"
    ff::run_scan_insert \
        top           $opt(top)           \
        netlist       $opt(netlist)       \
        lib_cells     $opt(lib_cells)     \
        clock_port    $opt(clock_port)    \
        scan_chains   $opt(scan_chains)   \
        scan_in       $opt(scan_in)       \
        scan_out      $opt(scan_out)      \
        scan_enable   $opt(scan_enable)   \
        max_chain_len $opt(max_chain_len) \
        clock_off     $opt(clock_off)

    # ---- Phase 2: INTEST ATPG (same session — no resume needed) ----------
    ff::log "--- Phase 2: INTEST scan ATPG ---"

    # set_testmode and run_atpg instead of ff::run_intest (which uses resume),
    # because the session is already loaded from Phase 1.
    set_testmode intest
    ff::log "test mode: [report_testmode]"

    set_option atpg.max_rounds          $opt(atpg_max_rounds)
    set_option atpg.sat_timeout_seconds $opt(sat_timeout)

    ff::log "running INTEST scan ATPG..."
    run_atpg -sa -scan -max $opt(atpg_max_rounds) -target $opt(atpg_target)

    set intest_result [status -scan]
    ff::log "INTEST done: [dict get $intest_result message]"
    report

    ff::log "###################################################"
    ff::log "core ATPG done: $opt(top)"
    ff::log "###################################################"

    return [dict create \
        top           $opt(top)    \
        intest_result $intest_result]
}

# =============================================================================
# CONFIGURATION — edit before running standalone
# (skipped when _ff_core_sourced is set by the importing script)
# =============================================================================
if {![info exists ::_ff_core_sourced]} {
    set cfg_top             "my_block"
    set cfg_netlist         "path/to/my_block.json"
    set cfg_lib_cells       "sky130"
    set cfg_clock_port      "clk"
    set cfg_scan_chains     4
    set cfg_scan_in         "scan_in"
    set cfg_scan_out        "scan_out"
    set cfg_scan_enable     "scan_en"
    set cfg_max_chain_len   0
    set cfg_clock_off       0
    set cfg_atpg_target     95.0
    set cfg_atpg_max_rounds 20
    set cfg_sat_timeout     10

    # ENTRY POINT (standalone only)
    ff::run_core_atpg \
        top             $cfg_top             \
        netlist         $cfg_netlist         \
        lib_cells       $cfg_lib_cells       \
        clock_port      $cfg_clock_port      \
        scan_chains     $cfg_scan_chains     \
        scan_in         $cfg_scan_in         \
        scan_out        $cfg_scan_out        \
        scan_enable     $cfg_scan_enable     \
        max_chain_len   $cfg_max_chain_len   \
        clock_off       $cfg_clock_off       \
        atpg_target     $cfg_atpg_target     \
        atpg_max_rounds $cfg_atpg_max_rounds \
        sat_timeout     $cfg_sat_timeout
}
