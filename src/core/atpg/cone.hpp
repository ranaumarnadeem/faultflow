#pragma once

#include <cstdint>
#include <vector>

#include "ir/compiled_graph/compiled_graph.hpp"

namespace faultflow::atpg {

// Per-fault cone of influence over a CompiledSimGraph (CompiledNetIndex space),
// used to restrict the dual-rail SAT miter to the structural region that matters
// (Tille/Eggersgluess/Drechsler, TCAD 2010, "circuit partitioning"):
//
//   in_outcone[n] : net n is in the fault's transitive fan-OUT — the fault
//                   effect can reach it, so it needs a FAULTY-machine variable.
//                   Outside the out-cone good == faulty by construction, so the
//                   faulty reference is aliased to the good var (no faulty CNF,
//                   no PI-equiv) — that is what shrinks the instance.
//   in_support[n] : net n is in the transitive fan-IN of the out-cone — it can
//                   influence a value the miter observes, so it needs a
//                   GOOD-machine variable.
//   reached_observables : the active observables the fault effect can reach (R).
//                   EMPTY => the fault drives nothing observable => structurally
//                   redundant; classify UNSAT without calling the solver.
struct FaultCone {
  std::vector<char> in_outcone;
  std::vector<char> in_support;
  std::vector<uint32_t> reached_observables;
};

// driver_of[net] = index of the node whose `out` == net, or -1 for a net with
// no driving node. INPUT (PI/source) nodes ARE drivers here but carry no inputs,
// so a backward walk terminates at them. Build once per graph.
std::vector<int> build_driver_index(const CompiledSimGraph& cg);

// Extract the cone for the stuck-at site `fault_net`. `observable` is a per-net
// flag (size >= net_count) of the ACTIVE observable set — cg.observable for the
// plain path, or mc.observable_nets for the mode-aware path. `driver` is from
// build_driver_index(cg). O(cone + net_count).
FaultCone extract_fault_cone(const CompiledSimGraph& cg, uint32_t fault_net,
                             const std::vector<int>& driver,
                             const std::vector<char>& observable);

}  // namespace faultflow::atpg
