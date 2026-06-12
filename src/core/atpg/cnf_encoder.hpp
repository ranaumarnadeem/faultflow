#pragma once

#include <cadical.hpp>
#include <vector>

#include "common/types.hpp"

namespace faultflow::atpg {

struct CnfVarMap {
  int next = 1;
  std::vector<int> free_vars;
  std::vector<int> faulty_vars;

  explicit CnfVarMap(int net_count);
};

void add_clause(CaDiCaL::Solver& solver, const std::vector<int>& clause);
void add_unit(CaDiCaL::Solver& solver, int lit);
void add_equiv(CaDiCaL::Solver& solver, int a, int b);
void add_gate_cnf(CaDiCaL::Solver& solver, GateType type,
                  const std::vector<int>& inputs, int output);
void add_xor_def(CaDiCaL::Solver& solver, int a, int b, int d);
void add_blocking_clause(CaDiCaL::Solver& solver,
                           const std::vector<int>& pi_literals,
                           const std::vector<bool>& assignment);

}  // namespace faultflow::atpg
