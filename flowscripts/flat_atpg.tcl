# =============================================================================
# flowscripts/flat_atpg.tcl
#
# Flat-netlist stuck-at ATPG for a single combinational or sequential design.
# No scan insertion or wrapper boundary cells.  Use this for leaf blocks or
# any design you want to characterise before adding scan.
#
# Usage:
#   python ff.py shell -f flowscripts/flat_atpg.tcl
#
# To run interactively, source this file in the shell and call ff::run_flat_atpg
# with the appropriate key-value pairs.
# =============================================================================

namespace eval ff {}

proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_flat_atpg
#
# Required arguments (key value):
#   top                 — module name
#   netlist             — path to .v (Verilog) or .json (Yosys JSON)
#   lib_cells           — PDK profile: sky130 | osu035
#   clock_port          — primary clock port name
#
# Optional arguments (key value):
#   clock_off           — inactive clock level: 0 (posedge) or 1 (negedge)  [0]
#   atpg_target         — stop when coverage reaches this percent           [95.0]
#   atpg_max_rounds     — hard cap on ATPG rounds                           [20]
#   sat_timeout         — per-fault SAT timeout in seconds                  [10]
#   blackbox_instances  — Tcl list of instances to blackbox (e.g. memories) [{}]
#
# Returns:
#   The status dict from the ATPG run (same as [status]).
# ---------------------------------------------------------------------------
proc ff::run_flat_atpg {args} {
    array set opt {
        clock_off          0
        atpg_target        95.0
        atpg_max_rounds    20
        sat_timeout        10
        blackbox_instances {}
    }
    array set opt $args
    foreach req {top netlist lib_cells clock_port} {
        if {![info exists opt($req)]} {
            error "run_flat_atpg: required argument '$req' not provided"
        }
    }

    ff::log "========================================================"
    ff::log "flat ATPG — top: $opt(top)  PDK: $opt(lib_cells)"
    ff::log "========================================================"

    # --- Project setup ---
    read_netlist $opt(netlist) -top $opt(top)
    use_lib_cells $opt(lib_cells)
    add_clock $opt(clock_port) -off $opt(clock_off)

    # Blackbox any opaque sub-blocks (SRAMs, PLLs, hard macros)
    foreach inst $opt(blackbox_instances) {
        ff::log "blackbox: $inst"
        add_blackbox $inst
    }

    # --- Synthesis (Verilog → gate-level JSON; no-op for pre-synthesised JSON) ---
    ff::log "synthesizing..."
    synth

    # --- Audit cell coverage against PDK — report only, never aborts ---
    ff::log "auditing cell coverage..."
    set audit [check_cells]
    if {![dict get $audit ok]} {
        ff::log "WARNING: uncovered cell types — run check_cells for details"
    }

    # --- Tune ATPG engine options ---
    set_option atpg.max_rounds       $opt(atpg_max_rounds)
    set_option atpg.sat_timeout_seconds $opt(sat_timeout)

    # --- Run stuck-at ATPG ---
    ff::log "running stuck-at ATPG..."
    ff::log "  target: $opt(atpg_target)%   max rounds: $opt(atpg_max_rounds)"
    run_atpg -sa -max $opt(atpg_max_rounds) -target $opt(atpg_target)

    # --- Results ---
    set result [status]
    ff::log "ATPG done: [dict get $result message]"
    report

    ff::log "========================================================"
    ff::log "done: $opt(top)"
    ff::log "========================================================"
    return $result
}

# =============================================================================
# CONFIGURATION — edit these variables before running
# =============================================================================
set cfg_top             "my_module"
set cfg_netlist         "path/to/my_module.v"   ;# .v for Verilog, .json pre-synthesised
set cfg_lib_cells       "sky130"                ;# sky130 | osu035
set cfg_clock_port      "clk"                   ;# top-level clock port
set cfg_clock_off       0                       ;# 0 = posedge / active-high
set cfg_atpg_target     95.0                    ;# stop when coverage reaches this %
set cfg_atpg_max_rounds 20                      ;# hard cap on rounds
set cfg_sat_timeout     10                      ;# per-fault SAT timeout (seconds)
set cfg_blackboxes      {}                      ;# e.g. {u_sram u_pll}

# =============================================================================
# ENTRY POINT
# =============================================================================
ff::run_flat_atpg \
    top                 $cfg_top             \
    netlist             $cfg_netlist         \
    lib_cells           $cfg_lib_cells       \
    clock_port          $cfg_clock_port      \
    clock_off           $cfg_clock_off       \
    atpg_target         $cfg_atpg_target     \
    atpg_max_rounds     $cfg_atpg_max_rounds \
    sat_timeout         $cfg_sat_timeout     \
    blackbox_instances  $cfg_blackboxes
