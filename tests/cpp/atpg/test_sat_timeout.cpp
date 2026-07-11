#include <catch2/catch_test_macros.hpp>

#include <cadical.hpp>

#include <chrono>

#include "atpg/sat_atpg.hpp"
#include "atpg/sat_terminator.hpp"

using namespace faultflow::atpg;
using steady = std::chrono::steady_clock;

namespace {

// Pigeonhole PHP(n): n+1 pigeons into n holes. UNSAT, and it genuinely requires
// CDCL search (not solved by root-level propagation), so CaDiCaL polls its
// terminator during solve -- exactly the path a wall-clock timeout must ride.
void add_pigeonhole(CaDiCaL::Solver& solver, int holes) {
  const int pigeons = holes + 1;
  auto var = [holes](int p, int h) { return p * holes + h + 1; };
  for (int p = 0; p < pigeons; ++p) {  // each pigeon in some hole
    for (int h = 0; h < holes; ++h) {
      solver.add(var(p, h));
    }
    solver.add(0);
  }
  for (int h = 0; h < holes; ++h) {  // no two pigeons share a hole
    for (int p1 = 0; p1 < pigeons; ++p1) {
      for (int p2 = p1 + 1; p2 < pigeons; ++p2) {
        solver.add(-var(p1, h));
        solver.add(-var(p2, h));
        solver.add(0);
      }
    }
  }
}

}  // namespace

// Documents the root cause of the fix: CaDiCaL has NO "time" search limit, so the
// previous `solver.limit("time", ...)` was a silent no-op (its false return was
// discarded) and the per-fault wall-clock SAT timeout was never enforced.
TEST_CASE("CaDiCaL has no 'time' limit -- only conflicts is valid here",
          "[atpg][timeout]") {
  CaDiCaL::Solver solver;
  REQUIRE(solver.is_valid_limit("conflicts"));
  REQUIRE_FALSE(solver.is_valid_limit("time"));
}

TEST_CASE("WallClockTerminator fires only once its deadline has passed",
          "[atpg][timeout]") {
  WallClockTerminator past(steady::now() - std::chrono::seconds(1));
  REQUIRE(past.terminate());

  WallClockTerminator future(steady::now() + std::chrono::hours(1));
  REQUIRE_FALSE(future.terminate());

  // deadline_in(N) is N seconds in the future, so it must not have passed yet.
  WallClockTerminator soon(WallClockTerminator::deadline_in(3600));
  REQUIRE_FALSE(soon.terminate());
}

TEST_CASE("map_cadical_result maps a limit interrupt to TIMEOUT", "[atpg][timeout]") {
  REQUIRE(map_cadical_result(0, /*limit_terminated=*/true) == SatSolveResult::TIMEOUT);
  REQUIRE(map_cadical_result(0, /*limit_terminated=*/false) == SatSolveResult::UNKNOWN);
  REQUIRE(map_cadical_result(10, false) == SatSolveResult::SAT);
  REQUIRE(map_cadical_result(20, false) == SatSolveResult::UNSAT);
}

TEST_CASE("An expired wall-clock terminator interrupts a real CaDiCaL solve",
          "[atpg][timeout]") {
  // A generous deadline lets the hard UNSAT instance finish normally...
  {
    CaDiCaL::Solver solver;
    add_pigeonhole(solver, 7);
    WallClockTerminator generous(steady::now() + std::chrono::hours(1));
    solver.connect_terminator(&generous);
    const int result = solver.solve();
    solver.disconnect_terminator();
    REQUIRE(result == 20);  // UNSAT
  }
  // ...while an already-expired deadline interrupts it: solve() returns 0, which
  // the ATPG layer maps to TIMEOUT. This is the wall-clock bound the old
  // limit("time", ...) no-op silently failed to provide -- without it a hard
  // fault ran unbounded until OOM.
  {
    CaDiCaL::Solver solver;
    add_pigeonhole(solver, 7);
    WallClockTerminator expired(steady::now() - std::chrono::seconds(1));
    solver.connect_terminator(&expired);
    const int result = solver.solve();
    solver.disconnect_terminator();
    REQUIRE(result == 0);  // interrupted before completion
    REQUIRE(map_cadical_result(result, true) == SatSolveResult::TIMEOUT);
  }
}
