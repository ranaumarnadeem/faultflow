#!/bin/bash
# FaultFlow against Fault on the 16 committed OSU035 cut netlists, at fixed thread
# counts, three repetitions a point. Each line of $OUT/results.log is one finished
# run, so a stopped sweep picks up where it was: run it again.
#
#   M            an extracted, built archive of the FaultFlow commit under test
#   FAULT_BIN    directory holding Fault's `fault` and `nl2bench`, patched as README.md says
#   FAULT_CELLS  Fault's OSU035 cell models, `Tech/osu035/osu035_stdcells.v` in its source
#   VENV         the Python venv FaultFlow runs in
#   OUT          where runs and results go
#   POINTS       tool:threads:repetition, run in order
#   SKIP_IDLE=1  start every run at once: a smoke test of the script, its times mean nothing
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
: "${M:?set M to an extracted, built FaultFlow archive}"
: "${FAULT_BIN:?set FAULT_BIN to the patched Fault build's bin directory}"
: "${FAULT_CELLS:?set FAULT_CELLS to Fault's Tech/osu035/osu035_stdcells.v}"
: "${VENV:?set VENV to the venv FaultFlow runs in}"
: "${OUT:?set OUT to the directory for runs and results}"
for f in "$M/ff.py" "$FAULT_BIN/fault" "$FAULT_BIN/nl2bench" "$FAULT_CELLS" "$VENV/bin/activate"; do
  [ -e "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
DESIGNS=${DESIGNS:-"c432 c499 c880 c1355 c1908 c2670 c3540 c5315 s27 s298 s344 s382 s526 s820 s1238 s1423"}
SEED=DEADCAFEDEADF00D
LIB=$M/cells/osu/osu035_stdcells.lib
source "$VENV/bin/activate"
export PYTHONWARNINGS=ignore
mkdir -p "$OUT"
LOG=$OUT/results.log

if [ -z "${POINTS:-}" ]; then
  POINTS=""
  for rep in 1 2 3; do
    for tool in faultflow quaigh prng; do POINTS="$POINTS $tool:10:$rep"; done
  done
  for rep in 1 2 3; do
    for tool in faultflow quaigh; do POINTS="$POINTS $tool:1:$rep"; done
  done
  # Fault's PRNG on one thread is by far the longest: one sample, last.
  POINTS="$POINTS prng:1:1"
fi

{
  echo "date $(date -Is)"
  echo "faultflow $(cat "$M/.commit" 2>/dev/null || echo unknown)"
  echo "fault $("$FAULT_BIN/fault" --version 2>&1 | head -1)"
  yosys -V
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

# The previous run's own load decays in about a minute: no need for five.
settle() {
  [ "${SKIP_IDLE:-0}" = 1 ] && return
  while ! awk -v l="$(cut -d' ' -f1 /proc/loadavg)" 'BEGIN{exit !(l < 1.5)}'; do sleep 10; done
}

run_faultflow() {
  local d=$1 threads=$2 W=$3 top clk
  case $d in c*) top=$d; clk=CLK;; s*) top=${d}_bench; clk=blif_clk_net;; esac
  python3 "$HERE/strip_clock.py" "$HERE/netlists/$d.cut.v" nl.v "$clk" || return 1
  cat > c.ofs <<OFS
[design]
netlist = $W/nl.json
top = $top
cell_lib = $M/cells/osu/osu035.json
liberty = $LIB

[fault_model]
type = stuck_at
collapsing = false
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
verify = false
sim_threads = $threads

[atpg]
tool = native
mode = comb
output = patterns.test
workers = $threads
random_vectors = 1000
compaction = dynamic
random_stop_coverage = 85.0

[report]
output = coverage.rpt
threshold = 95.0
OFS
  cat > timed.sh <<TIMED
yosys -q -p "read_liberty -lib $LIB; read_verilog nl.v; hierarchy -top $top; write_json nl.json" &&
python3 $M/ff.py init --top $top -c c.ofs &&
python3 $M/ff.py sim --top $top -c c.ofs --model stuck-at
TIMED
  /usr/bin/time -f '%e %U %S' -o time.txt bash timed.sh > run.log 2>&1
  local rc=$?
  python3 "$M/ff.py" status --top "$top" -c c.ofs > status.txt 2>&1
  local det den red vec
  det=$(grep -oE ' detected=[0-9]+' status.txt | head -1 | cut -d= -f2)
  den=$(grep -oE 'denominator=[0-9]+' status.txt | head -1 | cut -d= -f2)
  red=$(grep -oE ' redundant=[0-9]+' status.txt | head -1 | cut -d= -f2)
  vec=$(grep -oE 'sim complete .* vectors=[0-9]+' run.log | grep -oE ' vectors=[0-9]+' | head -1 | cut -d= -f2)
  RESULT="rc=$rc tries=1 faults=$((${den:-0}+${red:-0})) detected=${det:-} redundant=${red:-} vectors=${vec:-}"
}

run_fault() {
  local d=$1 gen=$2 threads=$3 bypass opts rc try
  case $d in c*) bypass="--clock CLK";; s*) bypass="--clock blif_clk_net --reset blif_reset_net";; esac
  if [ "$gen" = prng ]; then
    opts="-v 500 -r 500 -m 100 --ceiling 5000 --rng-seed $SEED"
  else
    "$FAULT_BIN/nl2bench" -o d.bench -l "$LIB" "$HERE/netlists/$d.cut.v" > nb.log 2>&1 || return 1
    opts="-g Quaigh -b d.bench"
  fi
  # Fault can die at start-up (SIGILL, see README.md): a retry's time is not counted.
  for try in $(seq 1 15); do
    FAULT_THREADS=$threads /usr/bin/time -f '%e %U %S' -o time.txt "$FAULT_BIN/fault" atpg \
      -c "$FAULT_CELLS" -o tv.json --output-covered cov.yml \
      $opts $bypass "$HERE/netlists/$d.cut.v" > run.log 2>&1
    rc=$?
    [ $rc -lt 128 ] && break
  done
  local sites cov tvs
  sites=$(grep -a -oE 'Found [0-9]+ fault sites' run.log | grep -oE '[0-9]+' | head -1)
  cov=$(grep -a -oE 'Simulations concluded: Coverage [0-9.]+' run.log | grep -oE '[0-9.]+$' | tail -1)
  tvs=$(grep -a -oE 'Compacted TV Count: [0-9]+' run.log | grep -oE '[0-9]+$' | tail -1)
  RESULT="rc=$rc tries=$try faults=$((2*${sites:-0})) coverage=${cov:-} vectors=${tvs:-}"
  rm -f tv.json
}

idled=0
for point in $POINTS; do
  IFS=: read -r tool threads rep <<< "$point"
  for d in $DESIGNS; do
    key="$tool $threads $rep $d "
    grep -q "^RESULT $key" "$LOG" 2>/dev/null && continue
    if [ $idled -eq 0 ]; then echo "WAIT $point $(date -Is)"; wait_idle; idled=1; else settle; fi
    W=$OUT/$tool/t$threads/r$rep/$d; rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 1
    load0=$(cut -d' ' -f1 /proc/loadavg)
    RESULT="rc=1 tries=0"
    if [ "$tool" = faultflow ]; then
      run_faultflow "$d" "$threads" "$W"
    else
      run_fault "$d" "$tool" "$threads"
    fi
    read -r wall user sys < <(tail -1 time.txt 2>/dev/null)
    echo "RESULT $key$RESULT wall=${wall:-} user=${user:-} sys=${sys:-} load0=$load0 end=$(date +%s)" | tee -a "$LOG"
  done
  idled=0
done
echo "ALL DONE $(date -Is)" | tee -a "$LOG"
