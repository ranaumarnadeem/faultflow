# picorv32a_intest_extest.tcl
# -----------------------------------------------------------------------
# Full INTEST + EXTEST stuck-at ATPG for PicoRV32a via the faultflow
# Tcl shell.  Exercises the complete shell command surface:
#   load_json  use_lib_cells  add_clock  report_clocks  wrap
#   check_cells  set_option  WORKERS  show_config
#   add_scan  check_scan  status  set_testmode  report_testmode
#   run_atpg  report
#
# Invoke from WSL project root:
#   source venv/bin/activate
#   setsid python3 ff.py shell --out output/picorv32a_tcl \
#       -f scripts/picorv32a_intest_extest.tcl \
#       > output/picorv32a_tcl.log 2>&1 &
#
# Prerequisite: examples/picorv32_synth/picorv32a_sky130.json must exist.
#   If missing: wsl yosys misc/synth_picorv32a_sky130.ys
# -----------------------------------------------------------------------
#
# NOTE on return-value layout:
# Every shell command returns a Tcl dict:
#   {status ok  command <name>  message <value>  ...metrics...}
# For OperationResult commands, additional fields (top, artifacts, warnings)
# appear alongside message. For plain-dict and string returns, message holds
# the raw value. Extract with: dict get [<cmd> ...] message
# -----------------------------------------------------------------------

puts "=================================================================="
puts "  picorv32a  INTEST + EXTEST  —  faultflow Tcl shell"
puts "=================================================================="

# -----------------------------------------------------------------------
# 1. Load pre-synthesised Yosys JSON and set technology profile.
# -----------------------------------------------------------------------
load_json examples/picorv32_synth/picorv32a_sky130.json -top picorv32a
use_lib_cells sky130

# Declare the clock.  off_state=0 → clk idles low during scan shift.
add_clock clk -off 0

# report_clocks returns: {status ok command report_clocks message {clocks {…}}}
# Navigate: dict get <result> message → {clocks {…}} ; then dict get … clocks
set clk_msg [dict get [report_clocks] message]
puts "Declared clocks: [dict get $clk_msg clocks]"

# -----------------------------------------------------------------------
# 2. Wrap I/O ports with IEEE-1500 WBR scan cells (scan model).
#    -o writes the wrapped JSON into the campaign output directory so the
#    synthesis artefact directory stays clean.  The session updates its
#    active netlist to point at the wrapped JSON automatically.
# -----------------------------------------------------------------------
puts "\n=== IEEE-1500 WBR wrapping ==="
wrap -model scan -clock clk -o output/picorv32a_tcl/picorv32a_sky130_wrapped.json
puts "wrap done"

# WBR boundary cells ($wbc_*) have no logic fault sites — blackbox them.
set_option simulation.unsupported_cells blackbox

# Cell audit on the wrapped netlist.  $wbc_* cells are explicitly allowed
# so audit reports them as acknowledged rather than missing.
set cc [dict get [check_cells -allow {$wbc_*}] message]
puts "check_cells: total=[dict get $cc total_cells]  ok=[dict get $cc ok]"

# -----------------------------------------------------------------------
# 3. Parallel workers and ATPG tuning.
# -----------------------------------------------------------------------
# WORKERS sets parallel SAT solver processes.
set w [WORKERS 4]
puts "Workers: [dict get $w message]"

# Escalating timeout tiers: 2 s (easy) → 10 s (medium) → 60 s (hard).
# A fault that times out at tier N is retried at tier N+1 next round.
set_option atpg.sat_timeout_schedule 2,10,60

# Cone-size ordering (default on): small-cone faults solve first, so the
# fast tier clears them and the hard tier budget goes to reconvergent faults.

# Maximum rounds before declaring STALLED.
set_option atpg.max_rounds 20

# Fault collapsing: equivalence-collapse primitive + AOI/OAI compound cells so the
# denominator and vector count shrink while coverage is preserved (collapses are
# detection-equivalent by construction). Sound for stuck-at scan INTEST.
set_option fault_model.collapsing true

# Coverage threshold used for the pass/fail gate in report.
set_option report.threshold 90.0

puts "\n--- Active configuration ---"
show_config
puts "---------------------------"

# -----------------------------------------------------------------------
# 4. Scan-chain insertion (4 chains) and structural validation.
# -----------------------------------------------------------------------
puts "\n=== Scan insertion (4 chains) ==="
add_scan -chains 4
check_scan
status -scan

# Clear any stale campaign from a previous run so ATPG starts fresh.
clean

# -----------------------------------------------------------------------
# 5. INTEST: scan ATPG on core logic + WBR boundary register.
# -----------------------------------------------------------------------
# set_testmode returns: {status ok command set_testmode message "test mode: …"}
set r [set_testmode intest]
puts "\n=================================================================="
puts "  [dict get $r message]"
puts "=================================================================="

set t0 [clock seconds]
if {[catch {run_atpg -scan -target 90.0} intest_r]} {
    puts "INTEST error: $intest_r"
} else {
    puts "INTEST done: [dict get $intest_r message]"
}
puts "INTEST elapsed: [expr {[clock seconds] - $t0}]s"
report

# -----------------------------------------------------------------------
# 6. EXTEST: combinational ATPG on the dead-core / interconnect view.
# -----------------------------------------------------------------------
set r [set_testmode extest]
puts "\n=================================================================="
puts "  [dict get $r message]"
puts "=================================================================="

set t0 [clock seconds]
if {[catch {run_atpg -scan -target 90.0} extest_r]} {
    puts "EXTEST error: $extest_r"
} else {
    puts "EXTEST done: [dict get $extest_r message]"
}
puts "EXTEST elapsed: [expr {[clock seconds] - $t0}]s"
report

puts "\n=================================================================="
puts "  Done."
puts "  DB: output/picorv32a_tcl/picorv32a/.faultflow/faultflow.sqlite"
puts "=================================================================="
