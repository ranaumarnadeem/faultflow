# =============================================================================
# flowscripts/scan.tcl
#
# Scan-insertion flow: synthesise a netlist, stitch generic scan chains,
# validate the chain structure, and export both a generic and a techmap-bound
# scanned netlist.  The session is checkpointed so that intest.tcl / core_atpg.tcl
# can resume from the scan state without re-running insertion.
#
# Usage (standalone):
#   python ff.py shell -f flowscripts/scan.tcl
#
# Usage (sourced by another script to import ff::run_scan_insert without running):
#   set _ff_scan_sourced 1
#   source {flowscripts/scan.tcl}
# =============================================================================

namespace eval ff {}

proc ff::log {msg} { puts "\[ff\] $msg" }

# ---------------------------------------------------------------------------
# ff::run_scan_insert
#
# Required arguments (key value):
#   top            — module name
#   netlist        — path to .v or .json
#   lib_cells      — PDK profile: sky130 | osu035
#   clock_port     — primary clock port name
#   scan_chains    — number of scan chains to create
#
# Optional arguments (key value):
#   scan_in        — scan-input port name          [scan_in]
#   scan_out       — scan-output port name         [scan_out]
#   scan_enable    — scan-enable port name         [scan_en]
#   max_chain_len  — max FFs per chain; 0=unlimited [0]
#   clock_off      — inactive clock level           [0]
#
# Side effects:
#   Saves session to output/<top>/.faultflow/session.json.
#   Writes scan_manifest.json + scan_chains.txt under output/<top>/.
#   Writes generic scanned JSON and (if supported) techmap-bound Verilog.
# ---------------------------------------------------------------------------
proc ff::run_scan_insert {args} {
    array set opt {
        scan_in       "scan_in"
        scan_out      "scan_out"
        scan_enable   "scan_en"
        max_chain_len 0
        clock_off     0
    }
    array set opt $args
    foreach req {top netlist lib_cells clock_port scan_chains} {
        if {![info exists opt($req)]} {
            error "run_scan_insert: required argument '$req' not provided"
        }
    }

    ff::log "========================================================"
    ff::log "scan insertion — top: $opt(top)  chains: $opt(scan_chains)"
    ff::log "========================================================"

    read_netlist $opt(netlist) -top $opt(top)
    use_lib_cells $opt(lib_cells)
    add_clock $opt(clock_port) -off $opt(clock_off)

    ff::log "synthesizing..."
    synth

    ff::log "auditing cell coverage..."
    check_cells

    # Dry-run: preview chain assignment without modifying the netlist
    ff::log "planning scan chains (dry-run)..."
    add_scan \
        -chains     $opt(scan_chains)   \
        -SI         $opt(scan_in)       \
        -SO         $opt(scan_out)      \
        -SE         $opt(scan_enable)   \
        -max_length $opt(max_chain_len) \
        -dry_run

    # Real insertion: stitch scan FFs into chains
    ff::log "inserting scan chains..."
    add_scan \
        -chains     $opt(scan_chains)   \
        -SI         $opt(scan_in)       \
        -SO         $opt(scan_out)      \
        -SE         $opt(scan_enable)   \
        -max_length $opt(max_chain_len)

    # Structural validation + normal-mode equivalence check
    ff::log "validating scan structure..."
    check_scan

    # Export generic (untechmapped) scanned JSON for ATPG / retarget
    ff::log "writing generic scanned netlist..."
    write_netlist -scan

    # Export techmap-bound netlist for place & route / PDK sign-off
    # (silently skipped if the PDK has no physical scan-cell binding)
    ff::log "writing techmapped scanned netlist..."
    if {[catch {write_netlist -scan -techmap} techmap_err]} {
        ff::log "note: techmap write skipped — $techmap_err"
    }

    # Checkpoint session so intest.tcl can resume without re-running scan
    save_session

    ff::log "scan insertion complete: $opt(scan_chains) chain(s) written"
    ff::log "========================================================"
    ff::log "done: $opt(top)"
    ff::log "========================================================"
}

# =============================================================================
# CONFIGURATION — edit before running standalone
# (skipped when _ff_scan_sourced is set by the importing script)
# =============================================================================
if {![info exists ::_ff_scan_sourced]} {
    set cfg_top           "my_module"
    set cfg_netlist       "path/to/my_module.json" ;# post-synthesis JSON or Verilog
    set cfg_lib_cells     "sky130"
    set cfg_clock_port    "clk"
    set cfg_scan_chains   4
    set cfg_scan_in       "scan_in"
    set cfg_scan_out      "scan_out"
    set cfg_scan_enable   "scan_en"
    set cfg_max_chain_len 0                        ;# 0 = no length limit

    # ENTRY POINT (standalone only)
    ff::run_scan_insert \
        top           $cfg_top           \
        netlist       $cfg_netlist       \
        lib_cells     $cfg_lib_cells     \
        clock_port    $cfg_clock_port    \
        scan_chains   $cfg_scan_chains   \
        scan_in       $cfg_scan_in       \
        scan_out      $cfg_scan_out      \
        scan_enable   $cfg_scan_enable   \
        max_chain_len $cfg_max_chain_len
}
