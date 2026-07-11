#pragma once

#include <cadical.hpp>

#include <chrono>

namespace faultflow::atpg {

// Wall-clock deadline terminator for CaDiCaL.
//
// CaDiCaL's `Solver::limit(name, val)` only accepts "conflicts", "decisions",
// "preprocessing", and "localsearch" -- there is NO "time" limit (its
// documented return value is `false` for an unrecognised name, and the earlier
// code discarded it, so `limit("time", ...)` was a silent no-op). A wall-clock
// SAT timeout must instead be enforced through a Terminator callback, which
// CaDiCaL polls during search: `terminate()` returning true interrupts `solve()`,
// which then returns 0 (UNKNOWN) and is mapped to TIMEOUT by map_cadical_result.
//
// Without this, the configurable per-fault SAT timeout (default 10 s, escalating
// schedule 2,10,60) never fired: a hard fault ran until the conflict limit, which
// on a large design means a very long, memory-heavy solve -- across parallel
// worker processes that is enough to trip the OOM killer and take the whole run
// (and the user's terminal) down.
class WallClockTerminator final : public CaDiCaL::Terminator {
 public:
  using clock = std::chrono::steady_clock;

  explicit WallClockTerminator(clock::time_point deadline) : deadline_(deadline) {}

  // Convenience: a deadline `seconds` from now.
  static clock::time_point deadline_in(int seconds) {
    return clock::now() + std::chrono::seconds(seconds);
  }

  bool terminate() override { return clock::now() >= deadline_; }

 private:
  clock::time_point deadline_;
};

}  // namespace faultflow::atpg
