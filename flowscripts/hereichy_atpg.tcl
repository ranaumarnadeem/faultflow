# =============================================================================
# flowscripts/hereichy_atpg.tcl
#
# Full hierarchical ATPG orchestration for a chip-level project.
#
# For each block:
#   1. Scan insertion       (ff::run_scan_insert)
#   2. INTEST scan ATPG     (INTEST, core internals + WBC inward)
#   3. Session is saved; reset for next block
#
# Then for the assembly:
#   4. EXTEST combinational ATPG (interconnect + WBC outward, cores blackboxed)
#
# Coverage aggregation (chip number) is produced by:
#   python ff.py project report -p <project.json>
#
# This script maps directly to a faultflow project manifest
# (project_*.json, schema faultflow_project_v1).  It is a convenience
# wrapper — the project CLI command does the same thing in Python.
#
# Usage:
#   python ff.py shell -f flowscripts/hereichy_atpg.tcl
# =============================================================================

# Import proc definitions from sub-scripts (guards prevent their entry points)
set ::_ff_scan_sourced   1
set ::_ff_intest_sourced 1
set ::_ff_extest_sourced 1
set ::_ff_core_sourced   1
source [file join [file dirname [info script]] core_atpg.tcl]
source [file join [file dirname [info script]] extest.tcl]

namespace eval ff {}
proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_hierarchical_atpg
#
# Drives the full chip ATPG flow: per-block core_atpg then assembly EXTEST.
#
# Required arguments (key value):
#   blocks           — Tcl list of block dicts.  Each dict must contain:
#                        name         block label (used in log output)
#                        top          module name
#                        netlist      path to .v or .json
#                        clock_port   primary clock port
#                        scan_chains  number of scan chains
#                      Optional per-block keys (same defaults as run_core_atpg):
#                        scan_in  scan_out  scan_enable  max_chain_len
#                        clock_off  atpg_target  atpg_max_rounds  sat_timeout
#   assembly_top     — assembly module name
#   assembly_netlist — path to assembly .json or .v
#   assembly_blackboxes — Tcl list of core instance names to blackbox
#   lib_cells        — PDK profile for all scopes: sky130 | osu035
#
# Optional arguments (key value):
#   assembly_clock_port  — clock in the assembly [clk]
#   atpg_target          — global default ATPG target % [95.0]
#   atpg_max_rounds      — global default ATPG round cap [20]
#   sat_timeout          — global default SAT timeout (s) [10]
#
# Returns:
#   dict with keys:
#     block_results    — list of per-block dicts {name top intest_result}
#     extest_result    — status dict from the assembly EXTEST run
# ---------------------------------------------------------------------------
proc ff::run_hierarchical_atpg {args} {
    array set opt {
        assembly_clock_port "clk"
        atpg_target         95.0
        atpg_max_rounds     20
        sat_timeout         10
    }
    array set opt $args
    foreach req {blocks assembly_top assembly_netlist assembly_blackboxes lib_cells} {
        if {![info exists opt($req)]} {
            error "run_hierarchical_atpg: required argument '$req' not provided"
        }
    }

    ff::log "########################################################"
    ff::log "hierarchical ATPG — [llength $opt(blocks)] block(s) + assembly"
    ff::log "########################################################"

    set block_results {}

    # ---- Per-block: scan insertion + INTEST ---------------------------------
    set block_index 0
    foreach block $opt(blocks) {
        incr block_index
        array set blk {
            scan_in         "scan_in"
            scan_out        "scan_out"
            scan_enable     "scan_en"
            max_chain_len   0
            clock_off       0
        }
        # Per-block overrides; then global defaults for ATPG tuning
        array set blk $block
        foreach {key} {atpg_target atpg_max_rounds sat_timeout} {
            if {![info exists blk($key)]} {
                set blk($key) $opt($key)
            }
        }

        ff::log "=========================================="
        ff::log "Block $block_index / [llength $opt(blocks)]: $blk(name)"
        ff::log "=========================================="

        # Discard any previous session before loading this block
        reset

        set block_result [ff::run_core_atpg \
            top             $blk(top)             \
            netlist         $blk(netlist)         \
            lib_cells       $opt(lib_cells)       \
            clock_port      $blk(clock_port)      \
            scan_chains     $blk(scan_chains)     \
            scan_in         $blk(scan_in)         \
            scan_out        $blk(scan_out)        \
            scan_enable     $blk(scan_enable)     \
            max_chain_len   $blk(max_chain_len)   \
            clock_off       $blk(clock_off)       \
            atpg_target     $blk(atpg_target)     \
            atpg_max_rounds $blk(atpg_max_rounds) \
            sat_timeout     $blk(sat_timeout)]

        lappend block_results [dict merge $block_result [dict create name $blk(name)]]

        ff::log "block $blk(name) INTEST: [dict get $block_result intest_result message]"
    }

    # ---- Assembly EXTEST ----------------------------------------------------
    ff::log "=========================================="
    ff::log "Assembly EXTEST: $opt(assembly_top)"
    ff::log "=========================================="

    reset

    set extest_result [ff::run_extest \
        top                $opt(assembly_top)        \
        netlist            $opt(assembly_netlist)    \
        lib_cells          $opt(lib_cells)           \
        clock_port         $opt(assembly_clock_port) \
        blackbox_instances $opt(assembly_blackboxes) \
        atpg_target        $opt(atpg_target)         \
        atpg_max_rounds    $opt(atpg_max_rounds)     \
        sat_timeout        $opt(sat_timeout)]

    ff::log "assembly EXTEST: [dict get $extest_result message]"

    # ---- Summary ------------------------------------------------------------
    ff::log "########################################################"
    ff::log "hierarchical ATPG summary"
    ff::log "########################################################"
    foreach br $block_results {
        set r [dict get $br intest_result]
        ff::log "  [dict get $br name] INTEST: [dict get $r message]"
    }
    ff::log "  assembly EXTEST: [dict get $extest_result message]"
    ff::log ""
    ff::log "Chip-level aggregate:"
    ff::log "  python ff.py project report -p <project.json>"
    ff::log "########################################################"

    return [dict create \
        block_results  $block_results  \
        extest_result  $extest_result]
}

# =============================================================================
# CONFIGURATION — edit before running
# =============================================================================

# Global settings (apply to all scopes unless overridden per block)
set cfg_lib_cells       "sky130"
set cfg_atpg_target     95.0
set cfg_atpg_max_rounds 20
set cfg_sat_timeout     10

# Assembly / interconnect
set cfg_assembly_top       "my_soc_top"
set cfg_assembly_netlist   "path/to/assembly.json"
set cfg_assembly_clock     "clk"
set cfg_assembly_blackboxes {u_coreA u_coreB}  ;# core instances in the assembly

# Block list — one dict per block.
# Add or remove entries.  Per-block keys override the global settings above.
set cfg_blocks {
    {
        name        blkA
        top         blkA
        netlist     path/to/blkA.json
        clock_port  clk
        scan_chains 2
    }
    {
        name        blkB
        top         blkB
        netlist     path/to/blkB.json
        clock_port  clk
        scan_chains 2
        atpg_target 90.0
    }
}

# =============================================================================
# ENTRY POINT
# =============================================================================
ff::run_hierarchical_atpg \
    blocks               $cfg_blocks              \
    assembly_top         $cfg_assembly_top        \
    assembly_netlist     $cfg_assembly_netlist    \
    assembly_blackboxes  $cfg_assembly_blackboxes \
    lib_cells            $cfg_lib_cells           \
    assembly_clock_port  $cfg_assembly_clock      \
    atpg_target          $cfg_atpg_target         \
    atpg_max_rounds      $cfg_atpg_max_rounds     \
    sat_timeout          $cfg_sat_timeout
