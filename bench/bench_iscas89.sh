#!/usr/bin/env bash
# bench_iscas89.sh — SA + transition scan ATPG on ISCAS-89 circuits (Sky130, sequential)
#
# Scan-chain count: chains = max(1, round(sqrt(FF_count)))
# Per-circuit chain allocations are in the CIRCUIT TABLE below.
#
# Two fault models per circuit:
#   stuck_at   — scan SA ATPG (pseudo-PI/PO from scan FFs)
#   transition — two-frame scan LOC transition ATPG
#
# Large circuits (s5378, s9234_1, s13207, s15850) are OFF by default because
# they can each take 30 min – several hours.  Enable with:
#   BENCH_LARGE=1 bash bench/bench_iscas89.sh 2>&1 | tee bench_iscas89_large.log
#
# Usage (from WSL, project root, venv active):
#   source venv/bin/activate
#   bash bench/bench_iscas89.sh 2>&1 | tee bench_iscas89.log
#
# Log parsing:
#   grep  'status=DONE'                bench_iscas89.log   # results
#   grep  'status=ERROR'               bench_iscas89.log   # failures
#   grep  'model=stuck_at.*DONE'       bench_iscas89.log   # SA only
#   grep  'model=transition.*DONE'     bench_iscas89.log   # trans only
#   awk   '/\[FF89\].*status=DONE/{print}' bench_iscas89.log
#
# Key fields in status=DONE lines:
#   circuit=  model=  ffs=  chains=  gates=  coverage=  vectors=  raw_vectors=
#   atpg_s=   sim_s=  total_s=  status=

set -uo pipefail

BENCH_LARGE=${BENCH_LARGE:-0}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CELL_LIB="cells/sky130/sky130_fd_sc_hd.json"
LIBERTY="cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
BENCH_DIR="tests/benchmarks/iscas89/synth_sky130"
FF_PY="python3 ff.py"
TAG="[FF89]"

ATPG_MAX_ROUNDS=30
SAT_TIMEOUT=10
COVERAGE_TARGET=95.0

# ---------------------------------------------------------------------------
# Circuit table
#
# chain = max(1, round(sqrt(ffs)))  — one chain per sqrt(FF) avoids long shift
# sequences that dominate test application time; too many chains wastes area.
#
# name       top_suffix   ffs  chains  gates
# (JSON = ${BENCH_DIR}/${name}_bench.json,  top module = ${name}_bench)
# s953 has 0 cells (empty stub) — skipped.
# ---------------------------------------------------------------------------

# Standard circuits: ~1–20 min each × 2 models
# Estimated total: 2–4 hours depending on machine
declare -A FFS=(
    [s27]=3      [s208_1]=8    [s298]=14   [s344]=15   [s349]=15
    [s382]=21    [s386]=6      [s400]=21   [s420_1]=16 [s444]=21
    [s510]=6     [s526]=21     [s526n]=21  [s641]=17   [s713]=17
    [s820]=5     [s832]=5      [s838_1]=32
    [s1196]=18   [s1238]=18    [s1423]=74
    [s1488]=6    [s1494]=6
)
declare -A CHAINS=(
    [s27]=1      [s208_1]=2    [s298]=4    [s344]=4    [s349]=4
    [s382]=4     [s386]=2      [s400]=4    [s420_1]=4  [s444]=4
    [s510]=2     [s526]=4      [s526n]=4   [s641]=4    [s713]=4
    [s820]=2     [s832]=2      [s838_1]=6
    [s1196]=4    [s1238]=4     [s1423]=8
    [s1488]=2    [s1494]=2
)
# Approximate gate count (total cells minus FFs); ? = unknown at script-write time
declare -A GATES=(
    [s27]=9      [s208_1]=37   [s298]=62   [s344]=82   [s349]=82
    [s382]=81    [s386]=81     [s400]=81   [s420_1]='?' [s444]=84
    [s510]=133   [s526]=91     [s526n]='?' [s641]=104  [s713]=92
    [s820]=151   [s832]=160    [s838_1]='?'
    [s1196]=291  [s1238]=302   [s1423]=361
    [s1488]=313  [s1494]=326
)
STANDARD_CIRCUITS=(
    s27 s208_1 s298 s344 s349
    s382 s386 s400 s420_1 s444
    s510 s526 s526n s641 s713
    s820 s832 s838_1
    s1196 s1238 s1423
    s1488 s1494
)

# Large circuits: 30 min – several hours each  (BENCH_LARGE=1 to enable)
# sqrt(162)=12.7→12  sqrt(135)=11.6→12  sqrt(452)=21.3→20  sqrt(559)=23.6→24
declare -A FFS_LARGE=(    [s5378]=162  [s9234_1]=135  [s13207]=452  [s15850]=559 )
declare -A CHAINS_LARGE=( [s5378]=12   [s9234_1]=12   [s13207]=20   [s15850]=24  )
declare -A GATES_LARGE=(  [s5378]=683  [s9234_1]='?'  [s13207]='?'  [s15850]='?' )
LARGE_CIRCUITS=(s5378 s9234_1 s13207 s15850)

MODELS=(stuck_at transition)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
ns()   { date +%s%N; }
elapsed_s() { echo "scale=3; ($(ns) - $1) / 1000000000" | bc; }

extract() {
    local key="$1" text="$2"
    echo "$text" | grep -oP "${key}=\K[0-9.]+[%]?" | sed 's/%//' | tail -1
}

# ---------------------------------------------------------------------------
# run_circuit NAME
#   Runs both SA and transition sim for a single ISCAS-89 circuit.
#   Looks up FFS/CHAINS/GATES from the appropriate declare -A table.
# ---------------------------------------------------------------------------
run_circuit() {
    local name="$1"
    local ffs chains gates

    if [[ -v "FFS[$name]" ]]; then
        ffs=${FFS[$name]}; chains=${CHAINS[$name]}; gates=${GATES[$name]}
    else
        ffs=${FFS_LARGE[$name]}; chains=${CHAINS_LARGE[$name]}; gates=${GATES_LARGE[$name]}
    fi

    local top="${name}_bench"
    local netlist="${BENCH_DIR}/${name}_bench.json"

    if [[ ! -f "$netlist" ]]; then
        echo "$TAG circuit=$name status=SKIP reason=netlist_missing"
        return
    fi

    for model in "${MODELS[@]}"; do
        total=$((total + 1))
        t_start=$(ns)

        # ---- fresh workspace --------------------------------------------------
        rm -rf "output/${top}"

        # ---- generate config --------------------------------------------------
        cfg=$(mktemp /tmp/ff89_XXXXXX.ofs)
        cat > "$cfg" <<OFS
[design]
netlist  = ${netlist}
top      = ${top}
cell_lib = ${CELL_LIB}
liberty  = ${LIBERTY}

[fault_model]
model      = ${model}
collapsing = false
launch     = loc
include_clock_faults = false
include_reset_faults = false

[simulation]
unsupported_cells = fail
verify = false

[atpg]
tool                = native
mode                = comb
max_rounds          = ${ATPG_MAX_ROUNDS}
sat_timeout_seconds = ${SAT_TIMEOUT}
compaction          = reverse

[wrap]
wbr_model = buffer

[scan]
chains           = ${chains}
max_chain_length =
scan_in          = scan_in
scan_out         = scan_out
scan_enable      = scan_en

[report]
threshold = ${COVERAGE_TARGET}
verbose   = true

[debug]
level = INFO
OFS

        echo "$TAG circuit=$name model=$model ffs=$ffs chains=$chains gates=$gates time=$(ts) status=START"

        # ---- init -------------------------------------------------------------
        if ! init_log=$($FF_PY init --top "$top" -c "$cfg" 2>&1); then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$name model=$model time=$(ts) total_s=$elapsed status=ERROR stage=init"
            echo "$init_log" | sed "s/^/$TAG  >> /"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi

        # ---- scan insertion ---------------------------------------------------
        echo "$TAG circuit=$name model=$model chains=$chains time=$(ts) status=SCAN_INSERT"
        if ! scan_log=$($FF_PY scan --top "$top" -c "$cfg" --scan-chains "$chains" 2>&1); then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$name model=$model time=$(ts) total_s=$elapsed status=ERROR stage=scan"
            echo "$scan_log" | sed "s/^/$TAG  >> /"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi
        echo "$scan_log" | sed "s/^/$TAG  | /"

        # ---- scan check -------------------------------------------------------
        if ! scanck_log=$($FF_PY scan-check --top "$top" -c "$cfg" 2>&1); then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$name model=$model time=$(ts) total_s=$elapsed status=ERROR stage=scan_check"
            echo "$scanck_log" | sed "s/^/$TAG  >> /"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi
        echo "$scanck_log" | sed "s/^/$TAG  | /"

        # ---- sim --scan -------------------------------------------------------
        sim_log=$($FF_PY sim --scan --top "$top" -c "$cfg" --model "${model//_/-}" 2>&1)
        sim_rc=$?
        echo "$sim_log" | sed "s/^/$TAG  | /"

        if [[ $sim_rc -ne 0 ]]; then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$name model=$model time=$(ts) total_s=$elapsed status=ERROR stage=sim exit=$sim_rc"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi

        # ---- extract metrics --------------------------------------------------
        elapsed=$(elapsed_s "$t_start")
        cov=$(    extract "coverage"           "$sim_log")
        vecs=$(   extract "\\bvectors"         "$sim_log")
        raw_v=$(  extract "raw_vectors"        "$sim_log")
        atpg_s=$( extract "atpg_seconds"       "$sim_log")
        sim_s=$(  extract "fault_sim_seconds"  "$sim_log")
        denom=$(  extract "denominator"        "$sim_log")
        det=$(    extract "detected"           "$sim_log")
        [[ -z "$raw_v" ]] && raw_v=$vecs

        echo "$TAG circuit=$name model=$model ffs=$ffs chains=$chains gates=$gates \
coverage=${cov:-N/A} denominator=${denom:-N/A} detected=${det:-N/A} \
vectors=${vecs:-N/A} raw_vectors=${raw_v:-N/A} \
atpg_s=${atpg_s:-N/A} sim_s=${sim_s:-N/A} total_s=$elapsed \
time=$(ts) status=DONE"

        n_pass=$((n_pass + 1))
        rm -f "$cfg"
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "$TAG suite=ISCAS-89 bench_large=$BENCH_LARGE cell_lib=sky130 time=$(ts) status=RUN_START"
total=0; n_pass=0; n_fail=0

for name in "${STANDARD_CIRCUITS[@]}"; do
    run_circuit "$name"
done

if [[ $BENCH_LARGE -eq 1 ]]; then
    echo "$TAG starting large circuits (BENCH_LARGE=1)"
    for name in "${LARGE_CIRCUITS[@]}"; do
        run_circuit "$name"
    done
else
    echo "$TAG large circuits skipped (set BENCH_LARGE=1 to include s5378 s9234_1 s13207 s15850)"
    echo "$TAG   estimated: s5378~30min, s9234_1~45min, s13207/s15850=hours each"
fi

echo "$TAG suite=ISCAS-89 total=$total passed=$n_pass failed=$n_fail time=$(ts) status=RUN_END"
