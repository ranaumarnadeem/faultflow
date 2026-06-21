#!/usr/bin/env bash
# bench_iscas85.sh — SA + transition ATPG on ISCAS-85 circuits (Sky130, combinational)
#
# ISCAS-85 are purely combinational (0 FFs).  No scan insertion is needed.
# Both fault models run per circuit:
#   stuck_at  — classic stuck-at 0/1 ATPG
#   transition — combinational broadside two-pattern (STR/STF), launch=loc
#
# Usage (from WSL, project root, venv active):
#   source venv/bin/activate
#   bash bench/bench_iscas85.sh 2>&1 | tee bench_iscas85.log
#
# Log parsing (after run):
#   grep  '^[FF85]'                bench_iscas85.log        # all structured lines
#   grep  'status=DONE'            bench_iscas85.log        # results only
#   grep  'status=ERROR'           bench_iscas85.log        # failures only
#   awk   '/\[FF85\].*status=DONE/{print $2,$6,$7,$8,$9,$10}' bench_iscas85.log
#
# Key fields in status=DONE lines:
#   circuit=  model=  ffs=  gates=  coverage=  vectors=  raw_vectors=
#   atpg_s=   sim_s=  total_s=  status=

set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CELL_LIB="cells/sky130/sky130_fd_sc_hd.json"
LIBERTY="cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
BENCH_DIR="tests/benchmarks/iscas85/synth_sky130"
FF_PY="python3 ff.py"
TAG="[FF85]"

ATPG_MAX_ROUNDS=30
SAT_TIMEOUT=10
COVERAGE_TARGET=95.0

# ---------------------------------------------------------------------------
# Circuit table: name  gates  (all combinational, 0 FFs, no scan chains)
# ---------------------------------------------------------------------------
declare -A GATES=( [c17]=3  [c432]=65  [c499]=160 )
CIRCUITS=(c17 c432 c499)
MODELS=(stuck_at transition)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
ns()   { date +%s%N; }
elapsed_s() { echo "scale=3; ($(ns) - $1) / 1000000000" | bc; }

extract() {
    # extract KEY from faultflow sim output (last match wins for multi-line)
    local key="$1" text="$2"
    echo "$text" | grep -oP "${key}=\K[0-9.]+[%]?" | sed 's/%//' | tail -1
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "$TAG suite=ISCAS-85 cell_lib=sky130 time=$(ts) status=RUN_START"
total=0; n_pass=0; n_fail=0

for top in "${CIRCUITS[@]}"; do
    netlist="${BENCH_DIR}/${top}.json"
    gates=${GATES[$top]}

    if [[ ! -f "$netlist" ]]; then
        echo "$TAG circuit=$top status=SKIP reason=netlist_missing"
        continue
    fi

    for model in "${MODELS[@]}"; do
        total=$((total + 1))
        t_start=$(ns)

        # ---- fresh workspace --------------------------------------------------
        rm -rf "output/${top}"

        # ---- generate config --------------------------------------------------
        cfg=$(mktemp /tmp/ff85_XXXXXX.ofs)
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

[report]
threshold = ${COVERAGE_TARGET}
verbose   = true

[debug]
level = INFO
OFS

        echo "$TAG circuit=$top model=$model ffs=0 gates=$gates chains=N/A time=$(ts) status=START"

        # ---- init -------------------------------------------------------------
        if ! init_log=$($FF_PY init --top "$top" -c "$cfg" 2>&1); then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$top model=$model time=$(ts) total_s=$elapsed status=ERROR stage=init"
            echo "$init_log" | sed "s/^/$TAG  >> /"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi

        # ---- sim --------------------------------------------------------------
        sim_log=$($FF_PY sim --top "$top" -c "$cfg" --model "${model//_/-}" 2>&1)
        sim_rc=$?
        # Echo full output so it's captured in the log file too
        echo "$sim_log" | sed "s/^/$TAG  | /"

        if [[ $sim_rc -ne 0 ]]; then
            elapsed=$(elapsed_s "$t_start")
            echo "$TAG circuit=$top model=$model time=$(ts) total_s=$elapsed status=ERROR stage=sim exit=$sim_rc"
            n_fail=$((n_fail + 1)); rm -f "$cfg"; continue
        fi

        # ---- extract metrics --------------------------------------------------
        elapsed=$(elapsed_s "$t_start")
        cov=$(     extract "coverage"           "$sim_log")
        vecs=$(    extract "\\bvectors"         "$sim_log")
        raw_v=$(   extract "raw_vectors"        "$sim_log")
        atpg_s=$(  extract "atpg_seconds"       "$sim_log")
        sim_s=$(   extract "fault_sim_seconds"  "$sim_log")
        denom=$(   extract "denominator"        "$sim_log")
        det=$(     extract "detected"           "$sim_log")

        # raw_vectors falls back to vectors when compaction made no change
        [[ -z "$raw_v" ]] && raw_v=$vecs

        echo "$TAG circuit=$top model=$model ffs=0 gates=$gates chains=N/A \
coverage=${cov:-N/A} denominator=${denom:-N/A} detected=${det:-N/A} \
vectors=${vecs:-N/A} raw_vectors=${raw_v:-N/A} \
atpg_s=${atpg_s:-N/A} sim_s=${sim_s:-N/A} total_s=$elapsed \
time=$(ts) status=DONE"

        n_pass=$((n_pass + 1))
        rm -f "$cfg"
    done
done

echo "$TAG suite=ISCAS-85 total=$total passed=$n_pass failed=$n_fail time=$(ts) status=RUN_END"
