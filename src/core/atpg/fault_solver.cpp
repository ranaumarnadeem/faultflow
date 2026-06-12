#include "atpg/fault_solver.hpp"

#include <cadical.hpp>

#include <algorithm>
#include <stdexcept>
#include <vector>

#include "atpg/cnf_encoder.hpp"

namespace faultflow::atpg {
namespace {

std::vector<uint32_t> inputs_of(const SimNode& node) {
  const uint32_t raw[] = {node.in0, node.in1, node.in2,
                          node.in3, node.in4, node.in5};
  std::vector<uint32_t> out;
  for (uint32_t input : raw) {
    if (input != UNUSED_INPUT) {
      out.push_back(input);
    }
  }
  return out;
}

std::vector<bool> assignment_from_pattern(const std::string& pattern) {
  std::vector<bool> out;
  out.reserve(pattern.size());
  for (char c : pattern) {
    if (c != '0' && c != '1') {
      throw std::runtime_error("invalid blocked pattern key");
    }
    out.push_back(c == '1');
  }
  return out;
}

void apply_solver_limits(CaDiCaL::Solver& solver, const SatSolveOptions& options) {
  if (options.conflict_limit >= 0) {
    solver.limit("conflicts", options.conflict_limit);
  }
  if (options.sat_timeout_seconds > 0) {
    solver.limit("time", static_cast<int64_t>(options.sat_timeout_seconds));
  }
}

bool had_solver_limits(const SatSolveOptions& options) {
  return options.conflict_limit >= 0 || options.sat_timeout_seconds > 0;
}

}  // namespace

std::vector<AtpgPiInfo> ordered_pis(const ParsedGraph& parsed,
                                    const CompiledSimGraph& cg) {
  std::vector<AtpgPiInfo> pis;
  const ParsedModule& mod = parsed.top_module();
  for (const auto& [name, port] : mod.ports) {
    if (port.direction != "input" || port.bits.size() != 1) {
      continue;
    }
    const int yid = port.bits.front();
    auto it = cg.yosys_to_compiled.find(yid);
    if (it == cg.yosys_to_compiled.end()) {
      continue;
    }
    pis.push_back({name, yid, static_cast<uint32_t>(it->second)});
  }
  std::sort(pis.begin(), pis.end(),
            [](const AtpgPiInfo& a, const AtpgPiInfo& b) { return a.name < b.name; });
  return pis;
}

std::string pattern_key(const std::map<std::string, bool>& vector,
                        const std::vector<AtpgPiInfo>& pis) {
  std::string key;
  key.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    const auto it = vector.find(pi.name);
    key.push_back(it != vector.end() && it->second ? '1' : '0');
  }
  return key;
}

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out) {
  CnfVarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  apply_solver_limits(solver, options);

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT) {
      continue;
    }
    const auto input_nets = inputs_of(node);
    std::vector<int> free_inputs;
    std::vector<int> faulty_inputs;
    free_inputs.reserve(input_nets.size());
    faulty_inputs.reserve(input_nets.size());
    for (uint32_t input : input_nets) {
      free_inputs.push_back(vars.free_vars.at(input));
      faulty_inputs.push_back(vars.faulty_vars.at(input));
    }
    add_gate_cnf(solver, node.type, free_inputs, vars.free_vars.at(node.out));
    if (node.out != fault.net_index) {
      add_gate_cnf(solver, node.type, faulty_inputs,
                   vars.faulty_vars.at(node.out));
    }
  }

  std::vector<int> pi_literals;
  pi_literals.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    pi_literals.push_back(vars.free_vars.at(pi.compiled));
    if (pi.compiled != fault.net_index) {
      add_equiv(solver, vars.free_vars.at(pi.compiled),
                vars.faulty_vars.at(pi.compiled));
    }
  }

  for (const std::string& blocked : options.blocked_patterns) {
    if (blocked.size() != pis.size()) {
      throw std::runtime_error("blocked pattern length mismatch");
    }
    add_blocking_clause(solver, pi_literals, assignment_from_pattern(blocked));
  }

  add_unit(solver, (fault.type == FaultType::SA1)
                       ? vars.faulty_vars.at(fault.net_index)
                       : -vars.faulty_vars.at(fault.net_index));

  std::vector<int> diff_vars;
  for (uint32_t obs : cg.observable) {
    const int d = vars.next++;
    add_xor_def(solver, vars.free_vars.at(obs), vars.faulty_vars.at(obs), d);
    diff_vars.push_back(d);
  }
  if (diff_vars.empty()) {
    throw std::runtime_error("SAT ATPG requires at least one observable output");
  }
  add_clause(solver, diff_vars);

  const int result = solver.solve();
  const SatSolveResult mapped =
      map_cadical_result(result, result == 0 && had_solver_limits(options));
  if (mapped != SatSolveResult::SAT) {
    return mapped;
  }

  out.clear();
  for (const AtpgPiInfo& pi : pis) {
    out[pi.name] = solver.val(vars.free_vars.at(pi.compiled)) > 0;
  }
  return SatSolveResult::SAT;
}

}  // namespace faultflow::atpg
