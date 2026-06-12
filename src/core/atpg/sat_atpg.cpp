#include "atpg/sat_atpg.hpp"

namespace faultflow::atpg {

SatSolveResult map_cadical_result(int cadical_status, bool limit_terminated) {
  if (cadical_status == 10) {
    return SatSolveResult::SAT;
  }
  if (cadical_status == 20) {
    return SatSolveResult::UNSAT;
  }
  if (limit_terminated) {
    return SatSolveResult::TIMEOUT;
  }
  return SatSolveResult::UNKNOWN;
}

}  // namespace faultflow::atpg
