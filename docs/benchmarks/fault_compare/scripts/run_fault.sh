#!/bin/bash
# Fault 0.9.4 ATPG on its own cut netlists (OSU035). Two generators: its default PRNG
# (run to 100% with a ceiling) and Quaigh. Fault's simulation intermittently dies with
# SIGILL at start-up (a race), so each run is retried.
set -u
cd ~/ffcmp
F=~/ffcmp/fault-patched/bin/fault
NB=~/ffcmp/fault-patched/bin/nl2bench
CEIL=${CEIL:-5000}
mkdir -p fault
DESIGNS=${DESIGNS:-"c432 c499 c880 c1355 c1908 c2670 c3540 c5315 s27 s298 s344 s382 s526 s820 s1238 s1423"}
for d in $DESIGNS; do
  case $d in c*) bypass="--clock CLK";; s*) bypass="--clock blif_clk_net --reset blif_reset_net";; esac
  $NB -o fault/$d.bench -l tech/osu035_stdcells.lib nl/$d.cut.v > fault/$d.nb.log 2>&1 || echo "NL2BENCH FAIL $d"
  for gen in prng quaigh; do
    if [ $gen = prng ]; then opts="-v 500 -r 500 -m 100 --ceiling $CEIL"; else opts="-g Quaigh -b fault/$d.bench"; fi
    for try in $(seq 1 15); do
      t0=$(date +%s.%N)
      $F atpg -c tech/osu035_stdcells.v -o fault/$d.$gen.tv.json --output-covered fault/$d.$gen.cov.yml \
        $opts $bypass nl/$d.cut.v > fault/$d.$gen.log 2>&1
      rc=$?
      t1=$(date +%s.%N)
      [ $rc -eq 0 ] && break
    done
    sites=$(grep -a -oE "Found [0-9]+ fault sites" fault/$d.$gen.log | grep -oE "[0-9]+")
    cov=$(grep -a -oE "Simulations concluded: Coverage [0-9.]+" fault/$d.$gen.log | grep -oE "[0-9.]+$")
    tvs=$(grep -a -oE "Compacted TV Count: [0-9]+" fault/$d.$gen.log | grep -oE "[0-9]+$")
    raw=$(grep -a -oE "Initial TV Count: [0-9]+" fault/$d.$gen.log | grep -oE "[0-9]+$")
    echo "RESULT $d $gen rc=$rc tries=$try sites=$sites faults=$((2*${sites:-0})) coverage=$cov raw_tvs=$raw tvs=$tvs wall=$(echo "$t1 - $t0" | bc)"
  done
done
