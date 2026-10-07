#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace faultflow::atpg {

inline constexpr uint64_t kAtpgRandomSeed = 0x5eed5eedULL;

enum class SatSolveResult : uint8_t {
  SAT = 0,
  UNSAT = 1,
  TIMEOUT = 2,
  UNKNOWN = 3,
};

// A (pseudo-)PI a scan-compression decompressor drives: its value is the XOR of
// these bits of the decompressor's seed (faultflow/scan/ring_generator.py::
// care_bit_rows). No bits: the value is 0.
struct SeededInput {
  uint32_t compiled = 0;  // CompiledNetIndex of the PI
  std::vector<int> seed_bits;
};

struct SatSolveOptions {
  int conflict_limit = -1;
  int sat_timeout_seconds = 10;
  std::vector<std::string> blocked_patterns;
  // Restrict each per-fault CNF to the fault's cone of influence (provably
  // verdict-equivalent to the whole-circuit miter). Default false keeps the
  // whole-circuit path as the A/B control and the equivalence oracle.
  bool cone_restrict = false;
  // Use the incremental fan-in cones (IFC) solver: encode one reached
  // observable's cone at a time instead of the whole cone up front. Verdict-
  // equivalent to the single-shot solver (proven by the equivalence test).
  bool incremental = false;
  // IFC fast untestability proof (FUP): node budget of the forward-bounded
  // region whose boundary cut is solved first to prove redundancy on a small
  // CNF. 0 disables FUP; only applied when the IFC solver is selected.
  int fup_region_budget = 32;
  // Scan compression: the solve gets seed_width free seed variables, and each
  // seeded input is the XOR of its seed bits -- in the only frame (stuck-at) or
  // the launch frame (transition) for seeded_inputs, in the capture frame for
  // seeded_capture_inputs (the launch-on-shift scan-in bits). So a SAT vector
  // is one the decompressor can load, and UNSAT means no seed tests the fault.
  // Every other PI stays free. Empty: no constraint.
  int seed_width = 0;
  std::vector<SeededInput> seeded_inputs;
  std::vector<SeededInput> seeded_capture_inputs;
  // A straight-line program over the seed (the continuous-flow decompressor's
  // ring, shift by shift): variable seed_width + j is the XOR of the variables
  // seed_defs[j] names, each an earlier one (none: 0). A seeded input's bits may
  // name any of the seed_width + seed_defs.size() variables, so the CNF grows
  // with the program, not with each cell's expanded row.
  std::vector<std::vector<int>> seed_defs;
  // When set, a SAT solve writes here the seed_width free seed bits it chose.
  std::vector<bool>* seed_out = nullptr;
};

// CaDiCaL mapping: 10=SAT, 20=UNSAT, 0=UNKNOWN. Interrupt from conflict/time
// budget maps to TIMEOUT when solver reports termination by limit, else UNKNOWN.
SatSolveResult map_cadical_result(int cadical_status, bool limit_terminated);

}  // namespace faultflow::atpg
