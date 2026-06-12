#include "atpg/cnf_encoder.hpp"

#include <stdexcept>

#include "sim/gate_eval.hpp"

namespace faultflow::atpg {

CnfVarMap::CnfVarMap(int net_count)
    : free_vars(net_count, 0), faulty_vars(net_count, 0) {
  for (int i = 0; i < net_count; ++i) {
    free_vars[i] = next++;
    faulty_vars[i] = next++;
  }
}

void add_clause(CaDiCaL::Solver& solver, const std::vector<int>& clause) {
  for (int lit : clause) {
    solver.add(lit);
  }
  solver.add(0);
}

void add_unit(CaDiCaL::Solver& solver, int lit) { add_clause(solver, {lit}); }

void add_equiv(CaDiCaL::Solver& solver, int a, int b) {
  add_clause(solver, {-a, b});
  add_clause(solver, {a, -b});
}

void add_gate_cnf(CaDiCaL::Solver& solver, GateType type,
                  const std::vector<int>& inputs, int output) {
  const int arity = static_cast<int>(inputs.size());
  if (arity > 6) {
    throw std::runtime_error("SAT ATPG supports at most 6-input primitives");
  }
  for (int mask = 0; mask < (1 << arity); ++mask) {
    std::vector<bool> bits;
    std::vector<int> clause;
    bits.reserve(arity);
    clause.reserve(static_cast<size_t>(arity) + 1);
    for (int i = 0; i < arity; ++i) {
      const bool value = (mask & (1 << i)) != 0;
      bits.push_back(value);
      clause.push_back(value ? -inputs[i] : inputs[i]);
    }
    const bool expected = eval_gate_scalar(type, bits);
    clause.push_back(expected ? output : -output);
    add_clause(solver, clause);
  }
}

void add_xor_def(CaDiCaL::Solver& solver, int a, int b, int d) {
  add_clause(solver, {-a, -b, -d});
  add_clause(solver, {a, b, -d});
  add_clause(solver, {-a, b, d});
  add_clause(solver, {a, -b, d});
}

void add_blocking_clause(CaDiCaL::Solver& solver,
                         const std::vector<int>& pi_literals,
                         const std::vector<bool>& assignment) {
  if (pi_literals.size() != assignment.size()) {
    throw std::runtime_error("blocking clause size mismatch");
  }
  std::vector<int> clause;
  clause.reserve(assignment.size());
  for (size_t i = 0; i < assignment.size(); ++i) {
    clause.push_back(assignment[i] ? -pi_literals[i] : pi_literals[i]);
  }
  add_clause(solver, clause);
}

}  // namespace faultflow::atpg
