#!/bin/bash
# picorv32a stuck-at Hybrid on main (~/cdm/src, aa34c10 code) with 10 SAT workers,
# whole-circuit encoding (cone_restrict = false) vs the fault's region only
# (cone_restrict = true, the default). Everything else as the paper's picorv32a run.
# One run at a time, each started only once the machine is idle.
set -u
R=/mnt/c/Users/Potato/Desktop/faultflow
M=$HOME/cdm/src
OUT=$HOME/ffcmp/cone_ab
source $R/venv/bin/activate
export PYTHONWARNINGS=ignore
mkdir -p $OUT

wait_idle() {
  local ok=0
  while [ $ok -lt 5 ]; do
    l=$(cut -d' ' -f1 /proc/loadavg)
    if awk -v l="$l" 'BEGIN{exit !(l < 1.0)}'; then ok=$((ok+1)); else ok=0; fi
    sleep 60
  done
}

for cr in ${CRS:-false true}; do
  W=$OUT/cone_$cr; rm -rf $W; mkdir -p $W; cd $W
  echo "WAIT cone_restrict=$cr $(date -Is)" | tee -a $OUT/summary.txt
  wait_idle
  cp $R/examples/picorv32_synth/picorv32a_sky130.json .
  cat > c.ofs <<EOF
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

[atpg]
tool = native
mode = comb
output = patterns.test
workers = 10
cone_restrict = $cr

[report]
output = coverage.rpt
threshold = 100.0
EOF
  echo "START cone_restrict=$cr $(date -Is) load=$(cat /proc/loadavg)" | tee -a $OUT/summary.txt
  (while true; do echo "$(date +%s) $(cut -d' ' -f1-3 /proc/loadavg)"; sleep 60; done) > load.log &
  LP=$!
  t0=$(date +%s)
  python3 $M/ff.py init --top picorv32a -c c.ofs > main.log 2>&1
  python3 $M/ff.py scan --top picorv32a -c c.ofs --scan-chains 162 --techmap >> main.log 2>&1
  python3 $M/ff.py scan-check --top picorv32a -c c.ofs >> main.log 2>&1
  t1=$(date +%s)
  python3 $M/ff.py sim --top picorv32a -c c.ofs --scan --export-patterns $W/patterns.json >> main.log 2>&1
  t2=$(date +%s)
  kill $LP
  python3 $M/ff.py status --top picorv32a -c c.ofs --scan > status.txt 2>&1
  st=$(grep -oE 'coverage=[^ ]+|detected=[0-9]+|redundant=[0-9]+|timeout=[0-9]+|atpg_terminal=[A-Z_]+|atpg_seconds=[^ ]+|fault_sim_seconds=[^ ]+' status.txt | tr '\n' ' ')
  vec=$(grep -oE 'sim complete .*vectors=[0-9]+' main.log | grep -oE ' vectors=[0-9]+')
  maxload=$(awk '{if ($2>m) m=$2} END{print m}' load.log)
  echo "RESULT cone_restrict=$cr | setup=$((t1-t0))s sim=$((t2-t1))s total=$((t2-t0))s |$vec | $st| maxload1m=$maxload" | tee -a $OUT/summary.txt
done
echo "ALL DONE $(date -Is)" | tee -a $OUT/summary.txt
