#include "atpg/sat_atpg.hpp"

#include <cadical.hpp>

#include <algorithm>
#include <cstdint>
#include <map>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "fault/collapser/fault_collapser.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/gate_eval.hpp"

namespace faultflow::atpg {
namespace {

struct PiInfo {
  std::string name;
  int yosys_id = 0;
  uint32_t compiled = 0;
};

struct VarMap {
  int next = 1;
  std::vector<int> free_vars;
  std::vector<int> faulty_vars;

  explicit VarMap(int net_count)
      : free_vars(net_count, 0), faulty_vars(net_count, 0) {
    for (int i = 0; i < net_count; ++i) {
      free_vars[i] = next++;
      faulty_vars[i] = next++;
    }
  }
};

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

bool should_skip_fault(const CompactFault& fault) {
  return fault.exclusion != FaultExclusion::NONE ||
         fault.collapsed_into != UINT32_MAX;
}

std::vector<PiInfo> ordered_pis(const ParsedGraph& parsed,
                                const CompiledSimGraph& cg) {
  std::vector<PiInfo> pis;
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
            [](const PiInfo& a, const PiInfo& b) { return a.name < b.name; });
  return pis;
}

std::string pattern_key(const std::map<std::string, bool>& vector,
                        const std::vector<PiInfo>& pis) {
  std::string key;
  key.reserve(pis.size());
  for (const PiInfo& pi : pis) {
    const auto it = vector.find(pi.name);
    key.push_back(it != vector.end() && it->second ? '1' : '0');
  }
  return key;
}

bool solve_fault(const CompiledSimGraph& cg, const std::vector<PiInfo>& pis,
                 const CompactFault& fault, int conflict_limit,
                 std::map<std::string, bool>& out) {
  VarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  if (conflict_limit > 0) {
    solver.limit("conflicts", conflict_limit);
  }

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

  for (const PiInfo& pi : pis) {
    if (pi.compiled != fault.net_index) {
      add_equiv(solver, vars.free_vars.at(pi.compiled),
                vars.faulty_vars.at(pi.compiled));
    }
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
  if (result != 10) {
    return false;
  }
  out.clear();
  for (const PiInfo& pi : pis) {
    out[pi.name] = solver.val(vars.free_vars.at(pi.compiled)) > 0;
  }
  return true;
}

}  // namespace

std::vector<std::map<std::string, bool>> generate_comb_sat_vectors(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& unsupported_policy, const SatAtpgOptions& options) {
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);
  if (!cg.ff_nodes.empty()) {
    throw std::runtime_error("native SAT ATPG is combinational-only in this slice");
  }

  EnumeratorOptions enum_options;
  enum_options.include_clock_faults = options.include_clock_faults;
  enum_options.include_reset_faults = options.include_reset_faults;
  std::vector<CompactFault> faults = enumerate_faults(ng, cg, enum_options);
  if (options.collapsing) {
    faults = collapse_primitive_faults(ng, cg, std::move(faults));
  }

  const std::vector<PiInfo> pis = ordered_pis(parsed, cg);
  if (pis.empty()) {
    throw std::runtime_error("native SAT ATPG requires single-bit primary inputs");
  }

  std::vector<std::map<std::string, bool>> vectors;
  std::set<std::string> seen;
  std::mt19937_64 rng(0x5eed5eedULL);
  for (int i = 0; i < options.random_vectors; ++i) {
    std::map<std::string, bool> vector;
    for (const PiInfo& pi : pis) {
      vector[pi.name] = (rng() & 1ULL) != 0;
    }
    const std::string key = pattern_key(vector, pis);
    if (seen.insert(key).second) {
      vectors.push_back(std::move(vector));
    }
  }

  for (const CompactFault& fault : faults) {
    if (should_skip_fault(fault)) {
      continue;
    }
    std::map<std::string, bool> vector;
    if (!solve_fault(cg, pis, fault, options.conflict_limit, vector)) {
      continue;
    }
    const std::string key = pattern_key(vector, pis);
    if (seen.insert(key).second) {
      vectors.push_back(std::move(vector));
      if (options.max_sat_vectors > 0 &&
          static_cast<int>(vectors.size()) >= options.max_sat_vectors) {
        break;
      }
    }
  }
  if (vectors.empty()) {
    throw std::runtime_error("native SAT ATPG generated no vectors");
  }
  return vectors;
}

}  // namespace faultflow::atpg
