#include "atpg/fault_solver.hpp"

#include <cadical.hpp>

#include <algorithm>
#include <functional>
#include <stdexcept>
#include <unordered_set>
#include <vector>

#include "atpg/cnf_encoder.hpp"
#include "atpg/cone.hpp"
#include "common/types.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"

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

// Cone masks driving the dual-rail miter. When `cone_restrict` is false the masks
// are all-ones and `observed` is the full observable set, so the encoding below
// is byte-identical to the whole-circuit miter (the A/B control). When true the
// masks come from the fault's cone of influence: nodes outside in_support get no
// good CNF; nodes outside in_outcone get no faulty CNF and their faulty value is
// aliased to the good var (good == faulty there by construction). An empty
// reached-observable set (`redundant`) means the fault drives nothing observable
// — structurally redundant, so the caller returns UNSAT without solving.
struct ConeMasks {
  std::vector<char> in_outcone;
  std::vector<char> in_support;
  std::vector<uint32_t> observed;
  bool redundant = false;
};

ConeMasks build_cone_masks(const CompiledSimGraph& cg, uint32_t fault_net,
                           const std::vector<uint32_t>& observable_set,
                           bool cone_restrict) {
  ConeMasks m;
  const size_t n = static_cast<size_t>(cg.net_count);
  if (cone_restrict) {
    std::vector<char> flag(n, 0);
    for (uint32_t obs : observable_set) {
      if (obs < static_cast<uint32_t>(cg.net_count)) {
        flag[obs] = 1;
      }
    }
    // Reuse the graph's precomputed driver index (topology-invariant); fall back
    // to building it only for a graph that predates the cached field.
    std::vector<int> fallback;
    const std::vector<int>* driver = &cg.driver_index;
    if (cg.driver_index.empty()) {
      fallback = build_driver_index(cg);
      driver = &fallback;
    }
    FaultCone cone = extract_fault_cone(cg, fault_net, *driver, flag);
    m.in_outcone = std::move(cone.in_outcone);
    m.in_support = std::move(cone.in_support);
    m.observed = std::move(cone.reached_observables);
    m.redundant = m.observed.empty();
  } else {
    m.in_outcone.assign(n, 1);
    m.in_support.assign(n, 1);
    m.observed = observable_set;
  }
  return m;
}

// Faulty-machine input literal: the faulty var inside the out-cone, else the
// good var (good == faulty outside the cone — the aliasing that shrinks the CNF).
inline int faulty_input_lit(const CnfVarMap& vars, const ConeMasks& masks,
                            uint32_t net) {
  return masks.in_outcone[net] ? vars.faulty_vars.at(net)
                               : vars.free_vars.at(net);
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
  // Phase 9: append controllable pseudo-PIs (e.g. blackbox output nets). They
  // are not backed by a real module port, so they get a synthesized name. They
  // MUST be in the SAT PI set so good==faulty is enforced (the miter cannot
  // cheat by differing them between machines). No-blackbox designs have none.
  std::vector<AtpgPiInfo> pseudo;
  pseudo.reserve(cg.pseudo_pi_nets.size());
  for (int cidx : cg.pseudo_pi_nets) {
    const int yid = cg.compiled_to_yosys[cidx];
    pseudo.push_back(
        {"__bbpi_" + std::to_string(yid), yid, static_cast<uint32_t>(cidx)});
  }
  std::sort(pseudo.begin(), pseudo.end(),
            [](const AtpgPiInfo& a, const AtpgPiInfo& b) { return a.name < b.name; });
  pis.insert(pis.end(), pseudo.begin(), pseudo.end());
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

std::string transition_pattern_key(const std::map<std::string, bool>& v1,
                                   const std::map<std::string, bool>& v2,
                                   const std::vector<AtpgPiInfo>& pis) {
  return pattern_key(v1, pis) + pattern_key(v2, pis);
}

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out) {
  CnfVarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  apply_solver_limits(solver, options);

  const std::vector<uint32_t> observable_set(cg.observable.begin(),
                                             cg.observable.end());
  const ConeMasks masks = build_cone_masks(cg, fault.net_index, observable_set,
                                           options.cone_restrict);
  if (masks.redundant) {
    return SatSolveResult::UNSAT;  // fault reaches no observable -> redundant
  }

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT) {
      continue;
    }
    const auto input_nets = inputs_of(node);
    if (masks.in_support[node.out]) {
      std::vector<int> free_inputs;
      free_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        free_inputs.push_back(vars.free_vars.at(input));
      }
      add_gate_cnf(solver, node.type, free_inputs, vars.free_vars.at(node.out));
    }
    if (masks.in_outcone[node.out] && node.out != fault.net_index) {
      std::vector<int> faulty_inputs;
      faulty_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        faulty_inputs.push_back(faulty_input_lit(vars, masks, input));
      }
      add_gate_cnf(solver, node.type, faulty_inputs,
                   vars.faulty_vars.at(node.out));
    }
  }

  std::vector<int> pi_literals;
  pi_literals.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    pi_literals.push_back(vars.free_vars.at(pi.compiled));
    if (pi.compiled != fault.net_index && masks.in_outcone[pi.compiled]) {
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
  for (uint32_t obs : masks.observed) {
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

// Incremental Fan-in Cones (IFC) stuck-at solver (Tille/Eggersgluess/Drechsler,
// TCAD 2010). Instead of encoding the whole fault cone + a miter over ALL reached
// observables up front, encode one reached observable's fan-in cone at a time and
// re-solve under a per-solve "differ at >= 1 observed-so-far" constraint. Most
// testable faults are classified after only a few observables, so far less CNF is
// built; learned conflict clauses carry across the (monotonic) augmentation steps.
// UNSAT only after every reached observable has been added => redundant. Verdict-
// identical to solve_stuck_at_fault when no solver limit is hit.
SatSolveResult solve_stuck_at_fault_incremental(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const CompactFault& fault, const SatSolveOptions& options,
    std::map<std::string, bool>& out) {
  const size_t n = static_cast<size_t>(cg.net_count);
  const uint32_t fault_net = fault.net_index;

  std::vector<char> is_obs(n, 0);
  for (int obs : cg.observable) {
    if (obs >= 0 && static_cast<size_t>(obs) < n) {
      is_obs[static_cast<size_t>(obs)] = 1;
    }
  }

  // Forward out-cone BFS (queue => reached observables in non-decreasing distance,
  // i.e. shortest-propagation-path order).
  std::vector<char> in_outcone(n, 0);
  std::vector<uint32_t> reached_obs;
  std::vector<uint32_t> bfs;
  size_t head = 0;
  in_outcone[fault_net] = 1;
  bfs.push_back(fault_net);
  if (is_obs[fault_net]) {
    reached_obs.push_back(fault_net);
  }
  while (head < bfs.size()) {
    const uint32_t net = bfs[head++];
    for (uint32_t i = cg.fanout_offsets[net]; i < cg.fanout_offsets[net + 1]; ++i) {
      const uint32_t t = cg.fanout_targets[i];
      if (!in_outcone[t]) {
        in_outcone[t] = 1;
        bfs.push_back(t);
        if (is_obs[t]) {
          reached_obs.push_back(t);
        }
      }
    }
  }
  if (reached_obs.empty()) {
    return SatSolveResult::UNSAT;  // reaches no observable => structurally redundant
  }

  CnfVarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  apply_solver_limits(solver, options);

  // Fault injection at the faulty site (permanent).
  add_unit(solver, (fault.type == FaultType::SA1)
                       ? vars.faulty_vars.at(fault_net)
                       : -vars.faulty_vars.at(fault_net));

  // Blocked patterns over PI good vars (permanent; identical to the baseline).
  if (!options.blocked_patterns.empty()) {
    std::vector<int> pi_literals;
    pi_literals.reserve(pis.size());
    for (const AtpgPiInfo& pi : pis) {
      pi_literals.push_back(vars.free_vars.at(pi.compiled));
    }
    for (const std::string& blocked : options.blocked_patterns) {
      if (blocked.size() != pis.size()) {
        throw std::runtime_error("blocked pattern length mismatch");
      }
      add_blocking_clause(solver, pi_literals, assignment_from_pattern(blocked));
    }
  }

  // enc[net] = the net's driver gate CNF has been added (good, plus faulty when the
  // net is in the out-cone). Doubles as the backward-walk visited marker.
  std::vector<char> enc(n, 0);
  std::vector<uint32_t> back;
  std::vector<int> diff_vars;

  const auto faulty_lit = [&](uint32_t net) -> int {
    return in_outcone[net] ? vars.faulty_vars.at(net) : vars.free_vars.at(net);
  };

  // Backward-encode the full fan-in cone of one observable: good gates for every
  // upstream net, faulty gates for the in-out-cone part (faulty inputs alias the
  // good var outside the out-cone), PI good==faulty for out-cone primary inputs.
  const auto encode_cone = [&](uint32_t root) {
    back.clear();
    back.push_back(root);
    while (!back.empty()) {
      const uint32_t net = back.back();
      back.pop_back();
      if (enc[net]) {
        continue;
      }
      enc[net] = 1;
      const int d = (net < cg.driver_index.size()) ? cg.driver_index[net] : -1;
      if (d < 0) {
        continue;  // net with no driver (should not occur post-compile)
      }
      const SimNode& node = cg.nodes[static_cast<size_t>(d)];
      if (node.type == GateType::INPUT) {
        if (in_outcone[net] && net != fault_net) {
          add_equiv(solver, vars.free_vars.at(net), vars.faulty_vars.at(net));
        }
        continue;  // PI/source: good var is the input, nothing upstream
      }
      const std::vector<uint32_t> input_nets = inputs_of(node);
      std::vector<int> good_inputs;
      good_inputs.reserve(input_nets.size());
      for (uint32_t in : input_nets) {
        good_inputs.push_back(vars.free_vars.at(in));
      }
      add_gate_cnf(solver, node.type, good_inputs, vars.free_vars.at(net));
      if (in_outcone[net] && net != fault_net) {
        std::vector<int> faulty_inputs;
        faulty_inputs.reserve(input_nets.size());
        for (uint32_t in : input_nets) {
          faulty_inputs.push_back(faulty_lit(in));
        }
        add_gate_cnf(solver, node.type, faulty_inputs, vars.faulty_vars.at(net));
      }
      for (uint32_t in : input_nets) {
        if (!enc[in]) {
          back.push_back(in);
        }
      }
    }
  };

  // FUP (fast untestability proof, Tille et al.): before the full IFC sweep, try
  // to disprove testability on a small forward-bounded neighbourhood of the fault.
  // The cut = the boundary of a depth-bounded region (region nets whose fanout
  // leaves the region, plus any in-region observable). It separates the fault from
  // every PO (a fault->PO path either leaves the region through a cut net or ends
  // at an in-region observable), and the region is self-contained for the faulty
  // machine (an out-cone input of a region gate sits at a smaller forward distance,
  // hence in-region), so if the fault cannot differ at ANY cut net it is redundant
  // — provable on a far smaller CNF than the full cone. Only worthwhile when the
  // region is a strict subset of the out-cone; the cut miter is a temporary
  // constrain, so it never leaks into the IFC solves below.
  const size_t fup_budget =
      options.fup_region_budget > 0
          ? static_cast<size_t>(options.fup_region_budget)
          : 0;
  if (fup_budget > 0 && bfs.size() > fup_budget) {
    std::vector<char> in_region(n, 0);
    for (size_t i = 0; i < fup_budget; ++i) {
      in_region[bfs[i]] = 1;
    }
    std::vector<int> cut_diffs;
    for (size_t i = 0; i < fup_budget; ++i) {
      const uint32_t r = bfs[i];
      bool is_cut = is_obs[r] != 0;
      if (!is_cut) {
        for (uint32_t f = cg.fanout_offsets[r]; f < cg.fanout_offsets[r + 1];
             ++f) {
          if (!in_region[cg.fanout_targets[f]]) {
            is_cut = true;
            break;
          }
        }
      }
      if (is_cut) {
        encode_cone(r);
        const int d = vars.next++;
        add_xor_def(solver, vars.free_vars.at(r), vars.faulty_vars.at(r), d);
        cut_diffs.push_back(d);
      }
    }
    if (!cut_diffs.empty()) {
      for (int dv : cut_diffs) {
        solver.constrain(dv);
      }
      solver.constrain(0);
      const int result = solver.solve();
      if (result == 20) {
        return SatSolveResult::UNSAT;  // redundant on the bounded cut
      }
      if (result == 0) {
        return map_cadical_result(result, had_solver_limits(options));
      }
      // result == 10 (SAT): the fault reaches the cut; fall through to full IFC,
      // reusing every gate already encoded above.
    }
  }

  // Add the reached observables' cones in PROGRESSIVE GROUPS (1, 2, 4, 8, then all
  // remaining in one final group => at most ~5 solves) rather than one at a time.
  // Each incremental solve carries the assumption overhead of the temporary cut
  // miter, so grouping bounds that overhead on faults with many reached observables
  // (the paper's "five steps" tradeoff) while staying verdict-identical: the final
  // group is always the whole observable set, so UNSAT there still means redundant.
  constexpr int kMaxGroups = 5;
  size_t k = 0;
  size_t group_size = 1;
  int groups_done = 0;
  while (k < reached_obs.size()) {
    const size_t group_end = (groups_done >= kMaxGroups - 1)
                                 ? reached_obs.size()  // final group: the rest
                                 : std::min(k + group_size, reached_obs.size());
    for (; k < group_end; ++k) {
      const uint32_t obs = reached_obs[k];
      encode_cone(obs);
      const int d = vars.next++;
      add_xor_def(solver, vars.free_vars.at(obs), vars.faulty_vars.at(obs), d);
      diff_vars.push_back(d);
    }
    ++groups_done;
    group_size *= 2;

    // "differ at >= 1 observable added so far" as a per-solve temporary clause.
    for (int dv : diff_vars) {
      solver.constrain(dv);
    }
    solver.constrain(0);
    const int result = solver.solve();
    if (result == 10) {  // SAT => testable
      out.clear();
      for (const AtpgPiInfo& pi : pis) {
        out[pi.name] = solver.val(vars.free_vars.at(pi.compiled)) > 0;
      }
      return SatSolveResult::SAT;
    }
    if (result == 0) {  // limit reached: unresolved (never redundant)
      return map_cadical_result(result, had_solver_limits(options));
    }
    // result == 20 (UNSAT here): cannot observe at the observables added so far;
    // add the next group's cones and retry, keeping learned clauses.
  }
  return SatSolveResult::UNSAT;  // all reached observables added, full instance UNSAT
}

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out,
                                    const ModeConfig& mc) {
  // FUNCTIONAL mode: fall back to the standard path with unchanged observation.
  if (mc.mode == TestMode::FUNCTIONAL) {
    return solve_stuck_at_fault(cg, pis, fault, options, out);
  }

  CnfVarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  apply_solver_limits(solver, options);

  // Build a fast-lookup set for safe-zero nets. In EXTEST mode these are the
  // TO_CORE outputs of WBR_IN cells (core inputs forced to 0 so the core is
  // decoupled from the interconnect under test).
  const std::unordered_set<uint32_t> safe_zero_set(mc.safe_zero_nets.begin(),
                                                    mc.safe_zero_nets.end());

  const ConeMasks masks = build_cone_masks(cg, fault.net_index,
                                           mc.observable_nets,
                                           options.cone_restrict);
  if (masks.redundant) {
    return SatSolveResult::UNSAT;  // fault reaches no observable -> redundant
  }

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT) {
      continue;
    }
    // EXTEST: WBR_IN.TO_CORE is a safe-zero net (core decoupled). Force it to 0
    // in whichever machine the cone keeps; outside the cone it is unreferenced.
    // A safe-zero net that side-feeds an out-cone gate is in `support`, so the
    // aliased faulty input reads the forced-0 good var -> still correct.
    if (node.type == GateType::WBR_IN && safe_zero_set.count(node.out)) {
      if (masks.in_support[node.out]) {
        add_unit(solver, -vars.free_vars.at(node.out));
      }
      if (masks.in_outcone[node.out] && node.out != fault.net_index) {
        add_unit(solver, -vars.faulty_vars.at(node.out));
      }
      continue;
    }
    const auto input_nets = inputs_of(node);
    if (masks.in_support[node.out]) {
      std::vector<int> free_inputs;
      free_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        free_inputs.push_back(vars.free_vars.at(input));
      }
      add_gate_cnf(solver, node.type, free_inputs, vars.free_vars.at(node.out));
    }
    if (masks.in_outcone[node.out] && node.out != fault.net_index) {
      std::vector<int> faulty_inputs;
      faulty_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        faulty_inputs.push_back(faulty_input_lit(vars, masks, input));
      }
      add_gate_cnf(solver, node.type, faulty_inputs,
                   vars.faulty_vars.at(node.out));
    }
  }

  std::vector<int> pi_literals;
  pi_literals.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    pi_literals.push_back(vars.free_vars.at(pi.compiled));
    if (pi.compiled != fault.net_index && masks.in_outcone[pi.compiled]) {
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

  // Mode-specific observable set, restricted to the reached observables (R).
  std::vector<int> diff_vars;
  for (uint32_t obs : masks.observed) {
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

namespace {

// Installs the launch<->capture PI coupling clauses for a two-frame transition
// CNF. Invoked after the per-PI capture good==faulty equivs are added. Broadside
// uses a no-op (launch and capture PIs independent); scan LOC couples each
// capture PPI to its launch PPO and holds the real PIs.
using PiCoupler = std::function<void(CaDiCaL::Solver&, const std::vector<int>&,
                                     CnfVarMap&)>;

// Shared two-frame transition CNF: three net-variable copies (launch good,
// capture good, capture faulty), the transition edge requirement + capture
// stuck-at force at the fault net, and the capture good-vs-faulty miter over the
// observable set. The only thing that varies between broadside and scan LOC is
// `couple_pis`; everything else is identical.
SatSolveResult solve_two_frame_transition(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const CompactFault& fault, const SatSolveOptions& options,
    const PiCoupler& couple_pis, bool launch_only_blocking,
    const std::vector<uint32_t>& extra_block_compiled,
    std::map<std::string, bool>& v1_out, std::map<std::string, bool>& v2_out) {
  // vars.free_vars / vars.faulty_vars are the CAPTURE-frame (frame 1) good and
  // faulty copies. Allocate a third copy for the LAUNCH frame (frame 0) good
  // machine; the launch frame needs no faulty copy because the fault is only
  // active in the capture frame — frame 0 exists solely to pin the good machine
  // pre-transition value at the fault net.
  CnfVarMap vars(cg.net_count);
  std::vector<int> launch_vars(static_cast<size_t>(cg.net_count), 0);
  for (int i = 0; i < cg.net_count; ++i) {
    launch_vars[i] = vars.next++;
  }

  CaDiCaL::Solver solver;
  apply_solver_limits(solver, options);

  // Cone masks restrict the two CAPTURE-frame machines (good + faulty); the
  // LAUNCH frame stays full because the scan LOC/LOS PI coupling forces every
  // capture PPI equal to its launch PPO regardless of the fault, so the launch
  // frame must be able to compute all coupled PPOs. Restricting 2 of 3 machines
  // is still verdict-preserving. Empty reached-observable set => redundant.
  const std::vector<uint32_t> observable_set(cg.observable.begin(),
                                             cg.observable.end());
  const ConeMasks masks = build_cone_masks(cg, fault.net_index, observable_set,
                                           options.cone_restrict);
  if (masks.redundant) {
    return SatSolveResult::UNSAT;
  }

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT) {
      continue;
    }
    const auto input_nets = inputs_of(node);
    // Launch frame: full good machine (no fault anywhere).
    std::vector<int> launch_inputs;
    launch_inputs.reserve(input_nets.size());
    for (uint32_t input : input_nets) {
      launch_inputs.push_back(launch_vars.at(input));
    }
    add_gate_cnf(solver, node.type, launch_inputs, launch_vars.at(node.out));
    // Capture good machine: over the fault's support.
    if (masks.in_support[node.out]) {
      std::vector<int> free_inputs;
      free_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        free_inputs.push_back(vars.free_vars.at(input));
      }
      add_gate_cnf(solver, node.type, free_inputs, vars.free_vars.at(node.out));
    }
    // Capture faulty machine: only inside the out-cone (faulty skips the fault
    // net; inputs outside the out-cone alias to the capture good var).
    if (masks.in_outcone[node.out] && node.out != fault.net_index) {
      std::vector<int> faulty_inputs;
      faulty_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        faulty_inputs.push_back(faulty_input_lit(vars, masks, input));
      }
      add_gate_cnf(solver, node.type, faulty_inputs,
                   vars.faulty_vars.at(node.out));
    }
  }

  // Capture-frame PIs: good == faulty everywhere except the fault net.
  std::vector<int> launch_pi_literals;
  std::vector<int> capture_pi_literals;
  launch_pi_literals.reserve(pis.size());
  capture_pi_literals.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    launch_pi_literals.push_back(launch_vars.at(pi.compiled));
    capture_pi_literals.push_back(vars.free_vars.at(pi.compiled));
    if (pi.compiled != fault.net_index && masks.in_outcone[pi.compiled]) {
      add_equiv(solver, vars.free_vars.at(pi.compiled),
                vars.faulty_vars.at(pi.compiled));
    }
  }

  // Launch<->capture PI coupling: broadside = none (independent); scan LOC =
  // capture PPI == launch PPO + held real PIs.
  couple_pis(solver, launch_vars, vars);

  // Broadside blocks the combined V1||V2 pair (length 2N); scan LOC blocks the
  // launch only (length N) because V2 is functionally derived from V1, so a new
  // launch is the only way to a new pair.
  std::vector<int> block_literals = launch_pi_literals;
  if (!launch_only_blocking) {
    block_literals.insert(block_literals.end(), capture_pi_literals.begin(),
                          capture_pi_literals.end());
  } else {
    // LOS: V2 = shift(V1) plus the free per-chain head scan-in bits, so the
    // launch alone is not unique — include the capture-frame head PPI vars.
    // (Empty for LOC, where V2 is fully derived from V1.)
    for (uint32_t idx : extra_block_compiled) {
      block_literals.push_back(vars.free_vars.at(idx));
    }
  }
  for (const std::string& blocked : options.blocked_patterns) {
    if (blocked.size() != block_literals.size()) {
      throw std::runtime_error("blocked pattern length mismatch");
    }
    add_blocking_clause(solver, block_literals,
                        assignment_from_pattern(blocked));
  }

  // Transition requirement at the fault net: the good machine must make the
  // required edge. STR (type==SA0): launch=0, capture=1. STF (type==SA1):
  // launch=1, capture=0.
  const bool capture_good = (fault.type == FaultType::SA1) ? false : true;
  const bool launch_good = !capture_good;
  add_unit(solver, launch_good ? launch_vars.at(fault.net_index)
                               : -launch_vars.at(fault.net_index));
  add_unit(solver, capture_good ? vars.free_vars.at(fault.net_index)
                                : -vars.free_vars.at(fault.net_index));

  // Capture-frame stuck-at force on the faulty copy (SA0 -> 0, SA1 -> 1).
  add_unit(solver, (fault.type == FaultType::SA1)
                       ? vars.faulty_vars.at(fault.net_index)
                       : -vars.faulty_vars.at(fault.net_index));

  // Miter: at least one reached observable differs between capture good/faulty.
  std::vector<int> diff_vars;
  for (uint32_t obs : masks.observed) {
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

  v1_out.clear();
  v2_out.clear();
  for (const AtpgPiInfo& pi : pis) {
    v1_out[pi.name] = solver.val(launch_vars.at(pi.compiled)) > 0;
    v2_out[pi.name] = solver.val(vars.free_vars.at(pi.compiled)) > 0;
  }
  return SatSolveResult::SAT;
}

}  // namespace

SatSolveResult solve_transition_fault(const CompiledSimGraph& cg,
                                      const std::vector<AtpgPiInfo>& pis,
                                      const CompactFault& fault,
                                      const SatSolveOptions& options,
                                      std::map<std::string, bool>& v1_out,
                                      std::map<std::string, bool>& v2_out) {
  // Broadside two-pattern: launch and capture PIs are independent, so no extra
  // coupling beyond the shared good==faulty capture equivs.
  const PiCoupler no_coupling =
      [](CaDiCaL::Solver&, const std::vector<int>&, CnfVarMap&) {};
  return solve_two_frame_transition(cg, pis, fault, options, no_coupling,
                                    /*launch_only_blocking=*/false,
                                    /*extra_block_compiled=*/{}, v1_out, v2_out);
}

SatSolveResult solve_scan_transition_fault(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LocCouple>& couples,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out) {
  const PiCoupler loc_coupling = [&](CaDiCaL::Solver& solver,
                                     const std::vector<int>& launch_vars,
                                     CnfVarMap& vars) {
    // Per scan FF: capture-frame current state (PPI) == launch-frame next-state
    // (PPO). This is the LOC functional-launch derivation V2 = f(V1). It is
    // installed on the GOOD capture machine even when the PPI is the fault net
    // (the faulty copy is independently forced to the stuck value).
    for (const LocCouple& c : couples) {
      add_equiv(solver, vars.free_vars.at(c.ppi_compiled),
                launch_vars.at(c.ppo_compiled));
    }
    // Real PIs hold launch->capture (single-clock LOC convention).
    for (uint32_t held : held_pi_compiled) {
      add_equiv(solver, vars.free_vars.at(held), launch_vars.at(held));
    }
  };
  // V2 is functionally derived from V1, so blocking the launch (V1) alone is
  // sufficient to force a new pair: launch-only N-bit blocked keys, matching the
  // stuck-at scan path's pattern_key handling.
  return solve_two_frame_transition(cg, pis, fault, options, loc_coupling,
                                    /*launch_only_blocking=*/true,
                                    /*extra_block_compiled=*/{}, v1_out, v2_out);
}

SatSolveResult solve_scan_los_transition_fault(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LosCouple>& couples,
    const std::vector<uint32_t>& head_ppi_compiled,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out) {
  const PiCoupler los_coupling = [&](CaDiCaL::Solver& solver,
                                     const std::vector<int>& launch_vars,
                                     CnfVarMap& vars) {
    // Per scan FF: capture-frame current state (PPI) == chain PREDECESSOR's
    // launch-frame current state (PPI). This is the shift relation V2[ff] =
    // V1[predecessor(ff)] created by the last scan-shift launch. Installed on the
    // GOOD capture machine even when the PPI is the fault net (the faulty copy is
    // independently forced to the stuck value). Chain HEAD PPIs get NO clause —
    // their capture value is the fresh launch scan-in bit (a free variable).
    for (const LosCouple& c : couples) {
      add_equiv(solver, vars.free_vars.at(c.capture_ppi_compiled),
                launch_vars.at(c.pred_ppi_compiled));
    }
    // Real PIs hold launch->capture (one functional capture clock after the
    // shift).
    for (uint32_t held : held_pi_compiled) {
      add_equiv(solver, vars.free_vars.at(held), launch_vars.at(held));
    }
  };
  // V2 = shift(V1) plus the free head scan-in bits, so the blocked key is
  // launch ‖ head-bits (launch_only_blocking with the head PPIs appended).
  return solve_two_frame_transition(cg, pis, fault, options, los_coupling,
                                    /*launch_only_blocking=*/true,
                                    head_ppi_compiled, v1_out, v2_out);
}

}  // namespace faultflow::atpg
