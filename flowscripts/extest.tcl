# =============================================================================
# flowscripts/extest.tcl
#
# IEEE 1500 EXTEST flow: test the interconnect between blocks in an assembly
# netlist.  Block cores are blackboxed (their boundary pseudo-PIs and pseudo-POs
# become controllable/observable) while the wrapper boundary cells (WBCs) and
# the interconnect wires are live and faultable.
#
# The assembly netlist must follow the faultflow project convention:
#   • Each block core is a cell instance (e.g. u_coreA, u_coreB).
#   • The WBC ring around each core is instantiated in the assembly and tagged
#     with faultflow_block + faultflow_wbc attributes.
#   • Only the core instances are blackboxed; the WBC rings are live.
#
# Usage (standalone):
#   python ff.py shell -f flowscripts/extest.tcl
#
# Usage (sourced by hereichy_atpg.tcl to import ff::run_extest):
#   set _ff_extest_sourced 1
#   source {flowscripts/extest.tcl}
# =============================================================================

namespace eval ff {}

proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_extest
#
# Required arguments (key value):
#   top                 — assembly module name
#   netlist             — path to assembly .json or .v
#   lib_cells           — PDK profile: sky130 | osu035
#   clock_port          — clock port in the assembly
#   blackbox_instances  — Tcl list of core instances to blackbox
#
# Optional arguments (key value):
#   clock_off           — inactive clock level       [0]
#   atpg_target         — coverage target in percent [90.0]
#   atpg_max_rounds     — hard cap on ATPG rounds    [20]
#   sat_timeout         — per-fault SAT timeout (s)  [10]
#
# Returns:
#   The status dict from [status] for this EXTEST run.
# ---------------------------------------------------------------------------
proc ff::run_extest {args} {
    array set opt {
        clock_off       0
        atpg_target     90.0
        atpg_max_rounds 20
        sat_timeout     10
    }
    array set opt $args
    foreach req {top netlist lib_cells clock_port blackbox_instances} {
        if {![info exists opt($req)]} {
            error "run_extest: required argument '$req' not provided"
        }
    }

    ff::log "========================================================"
    ff::log "EXTEST — assembly: $opt(top)  PDK: $opt(lib_cells)"
    ff::log "========================================================"

    read_netlist $opt(netlist) -top $opt(top)
    use_lib_cells $opt(lib_cells)
    add_clock $opt(clock_port) -off $opt(clock_off)

    # Blackbox each block core so the WBC ring + interconnect are live
    ff::log "blackboxing core instances..."
    foreach inst $opt(blackbox_instances) {
        ff::log "  blackbox: $inst"
        add_blackbox $inst
    }
    report_blackbox

    # Synthesize / validate
    ff::log "synthesizing assembly netlist..."
    synth

    ff::log "auditing cell coverage..."
    check_cells

    # EXTEST: WBC output cells drive interconnect from their FF q (loaded by scan
    # shift); WBC input cells observe interconnect into their FF q.
    set_testmode extest
    ff::log "test mode: [report_testmode]"

    set_option atpg.max_rounds          $opt(atpg_max_rounds)
    set_option atpg.sat_timeout_seconds $opt(sat_timeout)

    # Stuck-at combinational ATPG on the assembly (WBC-outward + interconnect faults)
    ff::log "running EXTEST stuck-at ATPG..."
    run_atpg -sa -max $opt(atpg_max_rounds) -target $opt(atpg_target)

    set result [status]
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
    set cfg_top              "my_soc_top"
    set cfg_netlist          "path/to/assembly.json"
    set cfg_lib_cells        "sky130"
    set cfg_clock_port       "clk"
    # List all core instances that appear in the assembly netlist
    set cfg_blackbox_insts   {u_coreA u_coreB}
    set cfg_clock_off        0
    set cfg_atpg_target      90.0
    set cfg_atpg_max_rounds  20
    set cfg_sat_timeout      10

    # ENTRY POINT (standalone only)
    ff::run_extest \
        top                $cfg_top             \
        netlist            $cfg_netlist         \
        lib_cells          $cfg_lib_cells       \
        clock_port         $cfg_clock_port      \
        blackbox_instances $cfg_blackbox_insts  \
        clock_off          $cfg_clock_off       \
        atpg_target        $cfg_atpg_target     \
        atpg_max_rounds    $cfg_atpg_max_rounds \
        sat_timeout        $cfg_sat_timeout
}
