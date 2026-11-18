#!/bin/bash
# picorv32a stuck-at Hybrid scan ATPG: SAT workers and fault-simulation threads,
# each varied with the other at 1, then both at 8. The baseline runs first and
# again last, so the difference between those two is the noise of one sample.
# One run at a time, each started only once the machine is idle. Each finished
# run appends one RESULT line to $OUT/summary.txt.
#
#   M            an extracted, built archive of the FaultFlow commit under test
#                (with its full SHA in $M/.commit)
#   VENV         the Python venv FaultFlow runs in
#   NETLIST      picorv32a_sky130.json (examples/picorv32_synth/, git-ignored)
#   OUT          where runs and results go
#   RUNS         workers,sim_threads pairs, run in order ("b" marks a repeat)
#   SKIP_IDLE=1  start every run at once: a smoke test of the script, its times mean nothing
set -u
: "${M:?set M to an extracted, built FaultFlow archive}"
: "${VENV:?set VENV to the venv FaultFlow runs in}"
: "${NETLIST:?set NETLIST to picorv32a_sky130.json}"
: "${OUT:?set OUT to the directory for runs and results}"
for f in "$M/ff.py" "$VENV/bin/activate" "$NETLIST"; do
  [ -e "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
source "$VENV/bin/activate"
export PYTHONWARNINGS=ignore
mkdir -p "$OUT"

{
  echo "date $(date -Is)"
  echo "faultflow $(cat "$M/.commit" 2>/dev/null || echo unknown)"
  uname -r
  lscpu | grep -E 'Model name|^CPU\(s\)'
  free -g | sed -n 2p
} > "$OUT/machine.txt"

wait_idle() {
  local ok=0
  [ "${SKIP_IDLE:-0}" = 1 ] && return
  while [ $ok -lt 5 ]; do
    l=$(cut -d' ' -f1 /proc/loadavg)
    if awk -v l="$l" 'BEGIN{exit !(l < 1.0)}'; then ok=$((ok+1)); else ok=0; fi
    sleep 60
  done
}

for run in ${RUNS:-1,1 1,2 1,4 1,8 2,1 4,1 8,1 8,8 1,1b}; do
  w=${run%,*}; s=${run#*,}; s=${s%b}
  grep -q "^RESULT workers=$w sim_threads=${run#*,} " "$OUT/summary.txt" 2>/dev/null && continue
  W=$OUT/w${w}_s${run#*,}; rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 1
  echo "WAIT workers=$w sim_threads=$s $(date -Is)" | tee -a "$OUT/summary.txt"
  wait_idle
  cp "$NETLIST" picorv32a_sky130.json
  cat > c.ofs <<OFS
[design]
netlist = $W/picorv32a_sky130.json
top = picorv32a
cell_lib = $M/cells/sky130/sky130_fd_sc_hd.json
liberty = $M/cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib

[fault_model]
type = stuck_at
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = blackbox
verify = false
sim_threads = $s

[atpg]
tool = native
mode = comb
output = patterns.test
workers = $w

[report]
output = coverage.rpt
threshold = 100.0
OFS
  echo "START workers=$w sim_threads=$s $(date -Is) load=$(cat /proc/loadavg)" | tee -a "$OUT/summary.txt"
  (while true; do echo "$(date +%s) $(cut -d' ' -f1-3 /proc/loadavg)"; sleep 60; done) > load.log &
  LP=$!
  t0=$(date +%s)
  python3 "$M/ff.py" init --top picorv32a -c c.ofs > main.log 2>&1
  python3 "$M/ff.py" scan --top picorv32a -c c.ofs --scan-chains 162 --techmap >> main.log 2>&1
  python3 "$M/ff.py" scan-check --top picorv32a -c c.ofs >> main.log 2>&1
  t1=$(date +%s)
  python3 "$M/ff.py" sim --top picorv32a -c c.ofs --scan --export-patterns "$W/patterns.json" >> main.log 2>&1
  rc=$?
  t2=$(date +%s)
  kill $LP
  python3 "$M/ff.py" status --top picorv32a -c c.ofs --scan > status.txt 2>&1
  st=$(grep -oE 'coverage=[^ ]+|detected=[0-9]+|redundant=[0-9]+|timeout=[0-9]+|atpg_terminal=[A-Z_]+|atpg_seconds=[^ ]+|fault_sim_seconds=[^ ]+' status.txt | tr '\n' ' ')
  vec=$(grep -oE 'sim complete .*vectors=[0-9]+' main.log | grep -oE ' vectors=[0-9]+' | head -1)
  # A gap in the samples well over 60 s means the machine slept during the run.
  maxload=$(awk '{if ($2>m) m=$2} END{print m}' load.log)
  maxgap=$(awk 'NR>1{g=$1-p; if (g>m) m=g} {p=$1} END{print m}' load.log)
  rm -f patterns.json
  echo "RESULT workers=$w sim_threads=${run#*,} | rc=$rc setup=$((t1-t0))s sim=$((t2-t1))s total=$((t2-t0))s |$vec | $st| maxload1m=$maxload maxgap=${maxgap}s" | tee -a "$OUT/summary.txt"
done
echo "ALL DONE $(date -Is)" | tee -a "$OUT/summary.txt"
