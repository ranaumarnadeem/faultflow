# =============================================================================
# flowscripts/intest.tcl
#
# IEEE 1500 INTEST flow: test the core internals of a wrapped + scan-inserted
# block by loading stimulus through the wrapper boundary (WBC input cells drive
# core inputs; output cells observe core outputs) and running stuck-at scan ATPG.
#
# Prerequisites:
#   1. The block must have been scan-inserted via scan.tcl and the session saved.
#      (ff::run_scan_insert calls save_session at the end.)
#   2. The netlist must have IEEE-1500 wrapper boundary cells ($wbc_in/out_*).
#
# Usage (standalone — resumes from a saved scan session):
#   python ff.py shell -f flowscripts/intest.tcl
#
# Usage (sourced by core_atpg.tcl / hereichy_atpg.tcl to import ff::run_intest):
#   set _ff_intest_sourced 1
#   source {flowscripts/intest.tcl}
# =============================================================================

namespace eval ff {}

proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_intest
#
# Resumes a saved scan session and runs stuck-at scan ATPG in INTEST mode.
# The session must have been saved by ff::run_scan_insert (or scan.tcl).
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
#   The scan status dict from [status -scan].
# ---------------------------------------------------------------------------
proc ff::run_intest {args} {
    array set opt {
        atpg_target     95.0
        atpg_max_rounds 20
        sat_timeout     10
    }
    array set opt $args
    if {![info exists opt(top)]} {
        error "run_intest: required argument 'top' not provided"
    }

    ff::log "========================================================"
    ff::log "INTEST — top: $opt(top)  target: $opt(atpg_target)%"
    ff::log "========================================================"

    # Restore synthesis + scan-check state from the scan session
    # (scan_inserted=true, scan_checked=true, netlist path, PDK profile)
    resume $opt(top)

    # Switch to INTEST mode: WBC input cells drive core inputs from their FF q
    # and WBC output cells observe core outputs; interconnect side is safe-forced.
    set_testmode intest
    ff::log "test mode: [report_testmode]"

    # Tune options
    set_option atpg.max_rounds          $opt(atpg_max_rounds)
    set_option atpg.sat_timeout_seconds $opt(sat_timeout)

    # Stuck-at scan ATPG in INTEST mode
    ff::log "running INTEST scan ATPG..."
    run_atpg -sa -scan -max $opt(atpg_max_rounds) -target $opt(atpg_target)

    set result [status -scan]
    ff::log "INTEST done: [dict get $result message]"
    report

    ff::log "========================================================"
    ff::log "done: $opt(top)"
    ff::log "========================================================"
    return $result
}

# =============================================================================
# CONFIGURATION — edit before running standalone
# (skipped when _ff_intest_sourced is set by the importing script)
# =============================================================================
if {![info exists ::_ff_intest_sourced]} {
    # 'top' must match the name used when scan.tcl was run so that resume
    # can locate the saved session in output/<top>/.faultflow/session.json
    set cfg_top             "my_module"
    set cfg_atpg_target     95.0
    set cfg_atpg_max_rounds 20
    set cfg_sat_timeout     10

    # ENTRY POINT (standalone only)
    ff::run_intest \
        top             $cfg_top             \
        atpg_target     $cfg_atpg_target     \
        atpg_max_rounds $cfg_atpg_max_rounds \
        sat_timeout     $cfg_sat_timeout
}
