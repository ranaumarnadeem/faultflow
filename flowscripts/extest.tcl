# =============================================================================
# flowscripts/extest.tcl
#
# IEEE 1500 EXTEST flow: test what is around a wrapped block's core -- its
# boundary cells and the ports they drive and capture -- by stuck-at scan ATPG
# on the block's graybox: the core a blackbox, the wrapper chains alone
# shifting. The output cells drive the ports from their flops; the input cells
# capture them.
#
# Prerequisites:
#   The block was wrapped and scan-inserted (scan.tcl with wrap 1, or
#   core_atpg.tcl) and the session saved (ff::run_scan_insert calls
#   save_session at the end).
#
# EXTEST at speed launches on shift (run_atpg -tf los): on capture it launches
# nothing at the boundary. The SoC around several wrapped blocks is tested by
# `ff.py project` (a faultflow_project_v2 manifest), not by this script.
#
# Usage (standalone -- resumes from a saved scan session):
#   python ff.py shell -f flowscripts/extest.tcl
#
# Usage (sourced, to import ff::run_extest):
#   set _ff_extest_sourced 1
#   source {flowscripts/extest.tcl}
# =============================================================================

namespace eval ff {}

proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_extest
#
# Resumes a saved scan session of a wrapped block and runs stuck-at scan ATPG
# in EXTEST mode.
#
# Required arguments (key value):
#   top             — module name (used to locate the saved session)
#
# Optional arguments (key value):
#   atpg_target     — coverage target in percent   [95.0]
#   atpg_max_rounds — hard cap on ATPG rounds      [20]
#   sat_timeout     — per-fault SAT timeout (sec)  [10]
#
# Returns:
#   The scan status dict from [status -scan]: EXTEST's campaign.
# ---------------------------------------------------------------------------
proc ff::run_extest {args} {
    array set opt {
        atpg_target     95.0
        atpg_max_rounds 20
        sat_timeout     10
    }
    array set opt $args
    if {![info exists opt(top)]} {
        error "run_extest: required argument 'top' not provided"
    }

    ff::log "========================================================"
    ff::log "EXTEST — top: $opt(top)  target: $opt(atpg_target)%"
    ff::log "========================================================"

    # Restore synthesis + scan-check state from the scan session, setting
    # aside a design already loaded (core_atpg.tcl's, say)
    reset
    resume $opt(top)

    # Switch to EXTEST: the mode pins held so the output cells drive the ports
    # from their flops and the input cells capture them; the core is unknown.
    set_testmode extest
    ff::log "test mode: [report_testmode]"

    set_option atpg.max_rounds          $opt(atpg_max_rounds)
    set_option atpg.sat_timeout_seconds $opt(sat_timeout)

    ff::log "running EXTEST scan ATPG..."
    run_atpg -sa -scan -max $opt(atpg_max_rounds) -target $opt(atpg_target)

    set result [status -scan]
    ff::log "EXTEST done: [dict get $result message]"
    report

    ff::log "========================================================"
    ff::log "done: $opt(top)"
    ff::log "========================================================"
    return $result
}

# =============================================================================
# CONFIGURATION — edit before running standalone
# (skipped when _ff_extest_sourced is set by the importing script)
# =============================================================================
if {![info exists ::_ff_extest_sourced]} {
    # 'top' must match the name used when scan.tcl was run so that resume
    # can locate the saved session in output/<top>/.faultflow/session.json
    set cfg_top             "my_module"
    set cfg_atpg_target     95.0
    set cfg_atpg_max_rounds 20
    set cfg_sat_timeout     10

    # ENTRY POINT (standalone only)
    ff::run_extest \
        top             $cfg_top             \
        atpg_target     $cfg_atpg_target     \
        atpg_max_rounds $cfg_atpg_max_rounds \
        sat_timeout     $cfg_sat_timeout
}
