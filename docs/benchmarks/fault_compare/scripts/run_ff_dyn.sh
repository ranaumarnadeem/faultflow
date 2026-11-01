#!/bin/bash
# FaultFlow on Fault's cut netlists (OSU035, collapsing off). The clock port is unused in
# a cut netlist (Fault holds it high and never faults it), so it is removed here.
set -u
REPO=$HOME/ffcmp/ff_main
WORKERS=${WORKERS:-1}
W=~/ffcmp/ff_dyn
mkdir -p $W/nl $W/cfg
cd $W
source /mnt/c/Users/Potato/Desktop/faultflow/venv/bin/activate
export PYTHONWARNINGS=ignore
DESIGNS=${DESIGNS:-"c432 c499 c880 c1355 c1908 c2670 c3540 c5315 s27 s298 s344 s382 s526 s820 s1238 s1423"}
for d in $DESIGNS; do
  case $d in c*) top=$d; clk=CLK;; s*) top=${d}_bench; clk=blif_clk_net;; esac
  python3 - ~/ffcmp/nl/$d.cut.v $W/nl/$d.v $clk <<'EOF'
import re, sys
src, dst, clk = sys.argv[1:]
t = open(src).read()
uses = len(re.findall(r"\b%s\b" % re.escape(clk), t))
assert uses == 3, (clk, uses)  # port list, input, wire: nothing else reads it
t = re.sub(r"^\s*%s,\n" % re.escape(clk), "", t, count=1, flags=re.M)
t = re.sub(r"^\s*(input|wire) %s;\n" % re.escape(clk), "", t, flags=re.M)
open(dst, "w").write(t)
EOF
  yosys -q -p "read_liberty -lib $REPO/cells/osu/osu035_stdcells.lib; read_verilog $W/nl/$d.v; hierarchy -top $top; write_json $W/nl/$d.json" \
    > $W/nl/$d.yosys.log 2>&1 || { echo "JSON FAIL $d"; continue; }
  cat > cfg/$d.ofs <<EOF
[design]
netlist = $W/nl/$d.json
top = $top
cell_lib = $REPO/cells/osu/osu035.json
liberty = $REPO/cells/osu/osu035_stdcells.lib

[fault_model]
type = stuck_at
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
verify = false

[atpg]
tool = native
mode = comb
output = patterns.test
workers = $WORKERS
random_vectors = 1000
compaction = dynamic
random_stop_coverage = 85.0

[report]
output = coverage.rpt
threshold = 95.0
EOF
  echo "##START $d"
  rm -rf output/$top
  t0=$(date +%s.%N)
  python3 $REPO/ff.py init --top $top -c cfg/$d.ofs
  python3 $REPO/ff.py sim --top $top -c cfg/$d.ofs --model stuck-at
  t1=$(date +%s.%N)
  echo "##WALL_S $(echo "$t1 - $t0" | bc)"
  python3 $REPO/ff.py status --top $top -c cfg/$d.ofs
  echo "##END $d"
done
