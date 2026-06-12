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

struct SatSolveOptions {
  int conflict_limit = -1;
  int sat_timeout_seconds = 10;
  std::vector<std::string> blocked_patterns;
};

// CaDiCaL mapping: 10=SAT, 20=UNSAT, 0=UNKNOWN. Interrupt from conflict/time
// budget maps to TIMEOUT when solver reports termination by limit, else UNKNOWN.
SatSolveResult map_cadical_result(int cadical_status, bool limit_terminated);

}  // namespace faultflow::atpg
