#include "atpg/fault_solver.hpp"

#include <cadical.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <functional>
#include <map>
#include <memory>
#include <stdexcept>
#include <unordered_set>
#include <utility>
#include <vector>

#include "atpg/cnf_encoder.hpp"
#include "atpg/cone.hpp"
#include "atpg/sat_terminator.hpp"
#include "common/types.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "sim/gate_eval.hpp"
#include "sim/xsim/dual.hpp"
#include "sim/xsim/logic.hpp"

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

// RAII: connects a wall-clock terminator for the duration of a fault's solve(s)
// and disconnects it before the terminator (or the solver) is destroyed. The
// deadline is set once when solving begins and spans every internal solve() call
// (the incremental solver calls solve() in a loop), so it bounds the TOTAL
// wall-clock budget per fault, matching the configured sat_timeout_seconds.
struct SolverTimeGuard {
  CaDiCaL::Solver* solver = nullptr;
  std::unique_ptr<WallClockTerminator> term;

  SolverTimeGuard() = default;
  SolverTimeGuard(SolverTimeGuard&&) = default;
  SolverTimeGuard& operator=(SolverTimeGuard&&) = default;
  SolverTimeGuard(const SolverTimeGuard&) = delete;
  SolverTimeGuard& operator=(const SolverTimeGuard&) = delete;
  ~SolverTimeGuard() {
    if (solver != nullptr && term) {
      solver->disconnect_terminator();
    }
  }
};

// Apply CaDiCaL search limits. The conflict limit is a native CaDiCaL limit; the
// wall-clock timeout is enforced via a Terminator (CaDiCaL has no "time" limit --
// see sat_terminator.hpp). Returns a guard the caller must keep alive for the
// whole solve; on its destruction the terminator is disconnected.
[[nodiscard]] SolverTimeGuard apply_solver_limits(CaDiCaL::Solver& solver,
                                                  const SatSolveOptions& options) {
  // Silence CaDiCaL's own diagnostic chatter (e.g. "c found falsified original
  // clause") so it never leaks into the tool's stdout; the ATPG layer reads the
  // solve() return code, not the solver's text output.
  solver.set("quiet", 1);
  if (options.conflict_limit >= 0) {
    solver.limit("conflicts", options.conflict_limit);
  }
  SolverTimeGuard guard;
  if (options.sat_timeout_seconds > 0) {
    guard.term = std::make_unique<WallClockTerminator>(
        WallClockTerminator::deadline_in(options.sat_timeout_seconds));
    guard.solver = &solver;
    solver.connect_terminator(guard.term.get());
  }
  return guard;
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
    std::vector<int> scratch;
    FaultCone cone = extract_fault_cone(
        cg, fault_net, driver_index_of(cg, scratch), flag);
    m.in_outcone = std::move(cone.in_outcone);
    m.in_support = std::move(cone.in_support);
    m.observed = std::move(cone.reached_observables);
    m.redundant = m.observed.empty();
  } else {
    m.in_outcone.assign(n, 1);
    m.in_support.assign(n, 1);
    m.observed = observable_set;
    // No observable at all (every one masked, e.g. behind unknown cells).
    m.redundant = m.observed.empty();
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

using NetVar = std::function<int(uint32_t)>;

// The XOR of `bits` of the seed variables `seed` (`what` names the user in an
// error): a chain of 2-input XOR definitions; 0 (no variable) for no bits.
int add_seed_xor(CaDiCaL::Solver& solver, CnfVarMap& vars,
                 const std::vector<int>& seed, const std::vector<int>& bits,
                 const std::string& what) {
  int value = 0;  // no seed bit yet: the constant 0
  for (int bit : bits) {
    if (bit < 0 || bit >= static_cast<int>(seed.size())) {
      throw std::runtime_error(what + ": seed variable " + std::to_string(bit) +
                               " is outside the " + std::to_string(seed.size()) +
                               " it may name");
    }
    const int seed_var = seed[static_cast<size_t>(bit)];
    if (value == 0) {
      value = seed_var;
      continue;
    }
    const int d = vars.next++;
    add_xor_def(solver, value, seed_var, d);
    value = d;
  }
  return value;
}

// Each seeded input's variable (`var_of` picks the frame) tied to the XOR of
// its seed bits, or 0 for no bits.
void add_seeded_inputs(CaDiCaL::Solver& solver, CnfVarMap& vars,
                       const std::vector<int>& seed,
                       const std::vector<SeededInput>& inputs,
                       const NetVar& var_of) {
  for (const SeededInput& input : inputs) {
    const int value = add_seed_xor(solver, vars, seed, input.seed_bits, "seeded input");
    const int target = var_of(input.compiled);
    if (value == 0) {
      add_unit(solver, -target);
    } else {
      add_equiv(solver, target, value);
    }
  }
}

// Scan compression (options.seed_width / seed_defs / seeded_inputs): ONE seed for
// the whole test, so both frames of a two-frame CNF read the same seed
// variables. `launch` is the only frame of a stuck-at CNF; `capture` is null
// there. Returns the seed_width free seed variables (none without seeded
// inputs), for read_seed.
std::vector<int> add_seed_constraints(CaDiCaL::Solver& solver, CnfVarMap& vars,
                                      const SatSolveOptions& options,
                                      const NetVar& launch, const NetVar* capture) {
  if (options.seed_width < 0) {
    throw std::runtime_error("seed width must be >= 0");
  }
  if (options.seeded_inputs.empty() && options.seeded_capture_inputs.empty()) {
    return {};
  }
  if (capture == nullptr && !options.seeded_capture_inputs.empty()) {
    throw std::runtime_error(
        "capture-frame seeded inputs need a two-frame (transition) solve");
  }
  std::vector<int> seed;
  seed.reserve(static_cast<size_t>(options.seed_width) + options.seed_defs.size());
  for (int i = 0; i < options.seed_width; ++i) {
    seed.push_back(vars.next++);
  }
  const std::vector<int> free_seed = seed;
  for (size_t j = 0; j < options.seed_defs.size(); ++j) {
    // Defined over the variables so far only: an earlier one.
    int value = add_seed_xor(solver, vars, seed, options.seed_defs[j],
                             "seed definition " + std::to_string(j));
    if (value == 0) {
      value = vars.next++;
      add_unit(solver, -value);
    }
    seed.push_back(value);
  }
  add_seeded_inputs(solver, vars, seed, options.seeded_inputs, launch);
  if (capture != nullptr) {
    add_seeded_inputs(solver, vars, seed, options.seeded_capture_inputs, *capture);
  }
  return free_seed;
}

// After a SAT solve: the free seed bits it chose, into options.seed_out.
void read_seed(CaDiCaL::Solver& solver, const SatSolveOptions& options,
               const std::vector<int>& free_seed) {
  if (options.seed_out == nullptr) {
    return;
  }
  options.seed_out->clear();
  for (int var : free_seed) {
    // A seed bit no clause names is free; the solver never saw it: 0.
    options.seed_out->push_back(var <= solver.vars() && solver.val(var) > 0);
  }
}

// A fault that reaches no observable, exported: the empty clause.
void export_structural_unsat(const SatSolveOptions& options) {
  CnfExport* out = options.cnf_export;
  if (out == nullptr) {
    return;
  }
  std::FILE* file = std::fopen(out->path.c_str(), "w");
  if (file == nullptr || std::fputs("p cnf 0 1\n0\n", file) < 0 ||
      std::fclose(file) != 0) {
    throw std::runtime_error("cannot write CNF file " + out->path);
  }
  *out = CnfExport{out->path};
  out->structurally_unsat = true;
}

// Before the solve: the recorded clauses (every one given to the solver)
// into options.cnf_export's file, with `max_var` in its header so a variable
// no clause names still exists there. `inputs` / `launch` are each PI's
// variable in the capture (only) frame / the launch frame.
void export_cnf(const std::vector<std::vector<int>>& clauses,
                const SatSolveOptions& options, int max_var,
                const std::vector<AtpgPiInfo>& pis,
                const std::function<int(uint32_t)>& inputs,
                const std::function<int(uint32_t)>& launch, int fault_var,
                const std::vector<int>& diff_vars) {
  CnfExport* out = options.cnf_export;
  if (out == nullptr) {
    return;
  }
  std::FILE* file = std::fopen(out->path.c_str(), "w");
  bool ok = file != nullptr &&
            std::fprintf(file, "p cnf %d %zu\n", max_var, clauses.size()) > 0;
  for (size_t i = 0; ok && i < clauses.size(); ++i) {
    for (int lit : clauses[i]) {
      ok = ok && std::fprintf(file, "%d ", lit) > 0;
    }
    ok = ok && std::fputs("0\n", file) >= 0;
  }
  if (file != nullptr && std::fclose(file) != 0) {
    ok = false;
  }
  if (!ok) {
    throw std::runtime_error("cannot write CNF file " + out->path);
  }
  *out = CnfExport{out->path};
  for (const AtpgPiInfo& pi : pis) {
    out->inputs.emplace_back(pi.name, inputs(pi.compiled));
    if (launch) {
      out->launch_inputs.emplace_back(pi.name, launch(pi.compiled));
    }
  }
  out->fault_var = fault_var;
  out->diff_vars = diff_vars;
}

}  // namespace

std::vector<AtpgPiInfo> ordered_pis(const ParsedGraph& parsed,
                                    const CompiledSimGraph& cg) {
  std::vector<AtpgPiInfo> pis;
  const ParsedModule& mod = parsed.top_module();
  for (const auto& [name, port] : mod.ports) {
    // A multi-bit top-level port (the locked synth script never runs
    // `splitnets`, so Yosys JSON keeps e.g. `input [3:0] b` as ONE port entry
    // with 4 bits) used to be collapsed to a single PI at its first bit
    // (bits.front()) regardless of width -- a deliberate prior fix for an
    // even worse bug (a dropped PI is UNCONSTRAINED in the CNF, an actual
    // soundness gap), but one that left bits 1..N-1 permanently
    // unaddressable: CaDiCaL still allocates and solves a genuine, independent
    // CNF variable for every one of those bits (CnfVarMap covers every
    // compiled net, not just `pis`), but any value it required for them was
    // silently discarded at witness extraction (below) and re-defaulted to 0
    // on replay -- the fault could be provably SAT internally yet fail every
    // Tier-A re-verification, deterministically, forever (the
    // `tier_a_reduced_mismatch` symptom). Fix: one PI per BIT for a
    // multi-bit port, named "<port>[<i>]" (a single-bit port keeps its exact
    // prior bare-name behavior, unchanged). This must stay byte-for-byte in
    // lockstep with the Python-side PI-name list
    // (faultflow/runner/runner.py's `_atpg_pi_names`, NOT `_port_names`) used
    // to size/persist `blocked_patterns` bit-strings -- a length or ordering
    // mismatch between the two independently-sorted lists throws the
    // length-mismatch guard below (or, worse, silently misreads a stale
    // pattern), which is exactly why the two sides share this positional
    // "<port>[<i>]" naming convention rather than depending on Yosys's
    // `netnames` table (this project's locked synthesis script does not
    // reliably populate a per-bit netname for a plain bus port -- confirmed
    // empirically against a real synthesized design).
    if (port.direction != "input" || port.bits.empty()) {
      continue;
    }
    if (port.bits.size() == 1) {
      const int yid = port.bits.front();
      auto it = cg.yosys_to_compiled.find(yid);
      if (it == cg.yosys_to_compiled.end()) {
        continue;
      }
      pis.push_back({name, yid, static_cast<uint32_t>(it->second)});
      continue;
    }
    for (size_t i = 0; i < port.bits.size(); ++i) {
      const int yid = port.bits[i];
      auto it = cg.yosys_to_compiled.find(yid);
      if (it == cg.yosys_to_compiled.end()) {
        continue;
      }
      pis.push_back({name + "[" + std::to_string(i) + "]", yid,
                     static_cast<uint32_t>(it->second)});
    }
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

namespace {

// A tristate driver or bus node (an X-mode graph only).
bool is_tristate(GateType type) {
  return type == GateType::TBUF_P || type == GateType::TBUF_N ||
         type == GateType::TINV_P || type == GateType::TINV_N ||
         type == GateType::BUS_MERGE || type == GateType::BUS_RES;
}

// out <-> sel ? if1 : if0.
void add_mux_cnf(CaDiCaL::Solver& solver, int sel, int if0, int if1, int out) {
  add_clause(solver, {sel, -if0, out});
  add_clause(solver, {sel, if0, -out});
  add_clause(solver, {-sel, -if1, out});
  add_clause(solver, {-sel, if1, -out});
}

// An X-mode graph's latch in one frame (the two-valued CNF): clear and preset
// forcing it, else D while the enable is active, else `state` -- a variable for
// its unknown power-up value, which the good and the faulty machine share. A
// missing clear or preset is 0. `truth` is a literal fixed true.
void add_latch_cnf(CaDiCaL::Solver& solver, int& next, int truth,
                   const CompiledFFConfig& cfg, int d, int enable, int clear,
                   int preset, int state, int out) {
  const auto active = [&](int lit, Polarity polarity) {
    if (lit == 0) {
      return -truth;
    }
    return polarity == Polarity::ACTIVE_HIGH ? lit : -lit;
  };
  const auto constant = [&](uint8_t v) { return v != 0 ? truth : -truth; };
  const int en = active(enable, cfg.enable_polarity);
  const int clr = active(clear, cfg.clear_polarity);
  const int pre = active(preset, cfg.preset_polarity);
  const int open = next++;
  add_mux_cnf(solver, en, state, d, open);
  const int unforced = next++;
  add_mux_cnf(solver, pre, open, constant(cfg.preset_value), unforced);
  const int cleared = next++;
  add_mux_cnf(solver, pre, constant(cfg.clear_value),
              constant(cfg.clear_preset_conflict_value), cleared);
  add_mux_cnf(solver, clr, unforced, cleared, out);
}

}  // namespace

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out) {
  CnfVarMap vars(cg.net_count);
  CaDiCaL::Solver solver;
  const auto time_guard = apply_solver_limits(solver, options);
  std::vector<std::vector<int>> clauses;
  const ClauseRecorder recorder(options.cnf_export != nullptr ? &clauses
                                                              : nullptr);

  const std::vector<uint32_t> observable_set(cg.observable.begin(),
                                             cg.observable.end());
  const ConeMasks masks = build_cone_masks(cg, fault.net_index, observable_set,
                                           options.cone_restrict);
  if (masks.redundant) {
    export_structural_unsat(options);
    return SatSolveResult::UNSAT;  // fault reaches no observable -> redundant
  }

  int latch_truth = 0;  // a literal fixed true, made for the first latch
  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT) {
      continue;
    }
    if (node.type == GateType::CONSTX) {
      // An x/z bit of an X-mode graph: an input the test cannot set, free in
      // the good machine and the same variable in the faulty one. A candidate
      // found through it is graded by the X engine, which alone credits it.
      if (masks.in_outcone[node.out] && node.out != fault.net_index) {
        add_equiv(solver, vars.free_vars.at(node.out), vars.faulty_vars.at(node.out));
      }
      continue;
    }
    if (node.type == GateType::LATCH) {
      if (latch_truth == 0) {
        latch_truth = vars.next++;
        add_unit(solver, latch_truth);
      }
      const CompiledFFConfig& cfg = cg.latch_configs.at(node.ff_cfg);
      const int state = vars.next++;  // its power-up value, shared
      const auto pins = [&](bool faulty) {
        std::array<int, 4> lits{};
        const std::array<uint32_t, 4> slots{node.in0, node.in1, node.in2, node.in3};
        for (size_t i = 0; i < slots.size(); ++i) {
          lits[i] = slots[i] == UNUSED_INPUT ? 0
                    : faulty                 ? faulty_input_lit(vars, masks, slots[i])
                                             : vars.free_vars.at(slots[i]);
        }
        return lits;
      };
      if (masks.in_support[node.out]) {
        const auto p = pins(false);
        add_latch_cnf(solver, vars.next, latch_truth, cfg, p[0], p[1], p[2], p[3], state,
                      vars.free_vars.at(node.out));
      }
      if (masks.in_outcone[node.out] && node.out != fault.net_index) {
        const auto p = pins(true);
        add_latch_cnf(solver, vars.next, latch_truth, cfg, p[0], p[1], p[2], p[3], state,
                      vars.faulty_vars.at(node.out));
      }
      continue;
    }
    if (is_tristate(node.type)) {
      // A bus has no two-valued value (Z, conflicts): left free in each
      // machine. A vector found through one is a candidate the X engine grades,
      // and an UNSAT through one proves nothing (solve_stuck_at_fault_x).
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
  const std::vector<int> free_seed = add_seed_constraints(
      solver, vars, options,
      [&](uint32_t net) { return vars.free_vars.at(net); }, nullptr);

  for (const std::string& blocked : options.blocked_patterns) {
    if (blocked.size() != pis.size()) {
      throw std::runtime_error(
          "blocked pattern length mismatch: pattern has " +
          std::to_string(blocked.size()) + " bits, expected " +
          std::to_string(pis.size()) +
          " PIs (C++/Python PI-list divergence -- check for an unmapped "
          "multi-bit-port bit)");
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
  export_cnf(
      clauses, options, vars.next - 1, pis,
      [&](uint32_t net) { return vars.free_vars.at(net); }, nullptr,
      vars.faulty_vars.at(fault.net_index), diff_vars);

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
  read_seed(solver, options, free_seed);
  return SatSolveResult::SAT;
}

namespace {

// One net in the dual-rail CNF: literal h (the net can be 1) and l (can be 0).
struct Rails {
  int h = 0;
  int l = 0;
};

// The nodes whose value an X-mode graph cannot know in one frame: an x/z bit of
// the netlist, a latch (holding its unknown power-up value while closed).
bool is_x_source(const SimNode& node) {
  return node.type == GateType::CONSTX || node.type == GateType::LATCH;
}

// out <-> OR over `terms` of the AND of each term's literals.
void add_or_of_ands(CaDiCaL::Solver& solver, int& next,
                    const std::vector<std::vector<int>>& terms, int out) {
  std::vector<int> any{-out};
  for (const std::vector<int>& lits : terms) {
    const int term = next++;
    std::vector<int> all{term};
    for (int lit : lits) {
      add_clause(solver, {-term, lit});
      all.push_back(-lit);
    }
    add_clause(solver, all);
    add_clause(solver, {-term, out});
    any.push_back(term);
  }
  add_clause(solver, any);
}

// out <-> OR over `cubes` of AND of the input literals each cube cares about
// (input i at 1 reads its h rail, at 0 its l rail): eval_gate_dual's own rule.
void add_cube_cover(CaDiCaL::Solver& solver, int& next, const std::vector<DualCube>& cubes,
                    const std::array<Rails, 6>& in, int out) {
  std::vector<std::vector<int>> terms;
  for (const DualCube& c : cubes) {
    std::vector<int> lits;
    for (int i = 0; i < 6; ++i) {
      if (((c.care >> i) & 1) != 0) {
        lits.push_back(((c.value >> i) & 1) != 0 ? in[static_cast<size_t>(i)].h
                                                 : in[static_cast<size_t>(i)].l);
      }
    }
    terms.push_back(std::move(lits));
  }
  add_or_of_ands(solver, next, terms, out);
}

// out <-> sel ? if1 : if0 on rails: dual_mux, exact for an unknown select.
void add_mux_rails(CaDiCaL::Solver& solver, int& next, Rails sel, Rails if0, Rails if1,
                   Rails out) {
  add_or_of_ands(solver, next, {{sel.l, if0.h}, {sel.h, if1.h}}, out.h);
  add_or_of_ands(solver, next, {{sel.l, if0.l}, {sel.h, if1.l}}, out.l);
}

// One node of a dual-rail CNF, the X engine's evaluation of it: `outr` its
// output's rails, `rails_of` its inputs', `held` a latch's held value (its
// unknown power-up value, or its value at the end of the frame before), `zero`
// and `one` the fixed rails.
template <typename RailsOf>
void encode_dual_node(CaDiCaL::Solver& solver, int& next, const CompiledSimGraph& cg,
                      const SimNode& node, Rails outr, const RailsOf& rails_of, Rails held,
                      Rails zero, Rails one) {
  if (node.type == GateType::LATCH) {
    // GoldenRefSimX's eval_latch: D while enabled, else the held value; clear
    // and preset forcing it.
    const CompiledFFConfig& cfg = cg.latch_configs.at(node.ff_cfg);
    const auto act = [](Rails r, Polarity p) {
      return p == Polarity::ACTIVE_HIGH ? r : Rails{r.l, r.h};
    };
    const auto constant = [&](uint8_t v) { return v != 0 ? one : zero; };
    const Rails en = act(rails_of(node.in1), cfg.enable_polarity);
    const Rails clr = (cfg.has_clear && node.in2 != UNUSED_INPUT)
                          ? act(rails_of(node.in2), cfg.clear_polarity)
                          : zero;
    const Rails pre = (cfg.has_preset && node.in3 != UNUSED_INPUT)
                          ? act(rails_of(node.in3), cfg.preset_polarity)
                          : zero;
    const Rails open{next++, next++};
    add_mux_rails(solver, next, en, held, rails_of(node.in0), open);
    const Rails unforced{next++, next++};
    add_mux_rails(solver, next, pre, open, constant(cfg.preset_value), unforced);
    const Rails cleared{next++, next++};
    add_mux_rails(solver, next, pre, constant(cfg.clear_value),
                  constant(cfg.clear_preset_conflict_value), cleared);
    add_mux_rails(solver, next, clr, unforced, cleared, outr);
    return;
  }
  if (node.type == GateType::TBUF_P || node.type == GateType::TBUF_N ||
      node.type == GateType::TINV_P || node.type == GateType::TINV_N) {
    // eval_tbuf_dual: the data while enabled, Z (0,0) while disabled, X while
    // the enable is unknown.
    Rails data = rails_of(node.in0);
    Rails en = rails_of(node.in1);
    if (node.type == GateType::TINV_P || node.type == GateType::TINV_N) {
      data = Rails{data.l, data.h};
    }
    if (node.type == GateType::TBUF_N || node.type == GateType::TINV_N) {
      en = Rails{en.l, en.h};
    }
    add_or_of_ands(solver, next, {{en.h, -en.l, data.h}, {en.h, en.l}}, outr.h);
    add_or_of_ands(solver, next, {{en.h, -en.l, data.l}, {en.h, en.l}}, outr.l);
    return;
  }
  if (node.type == GateType::BUS_MERGE || node.type == GateType::BUS_RES) {
    // eval_bus_dual: the union of the drivers; BUS_RES floating reads X
    // (h = some driver's h, or none can be 0; l likewise).
    std::vector<std::vector<int>> hs;
    std::vector<std::vector<int>> ls;
    std::vector<int> none_low;
    std::vector<int> none_high;
    for (uint32_t slot : {node.in0, node.in1, node.in2, node.in3, node.in4, node.in5}) {
      if (slot == UNUSED_INPUT) {
        continue;
      }
      const Rails d = rails_of(slot);
      hs.push_back({d.h});
      ls.push_back({d.l});
      none_low.push_back(-d.l);
      none_high.push_back(-d.h);
    }
    if (node.type == GateType::BUS_RES) {
      hs.push_back(none_low);
      ls.push_back(none_high);
    }
    add_or_of_ands(solver, next, hs, outr.h);
    add_or_of_ands(solver, next, ls, outr.l);
    return;
  }
  const DualCubes& cubes = dual_cubes(node.type);
  if (!cubes.valid) {
    throw std::runtime_error("dual-rail SAT ATPG: a node with no combinational function");
  }
  std::array<Rails, 6> in{};
  const std::array<uint32_t, 6> slots{node.in0, node.in1, node.in2,
                                      node.in3, node.in4, node.in5};
  for (size_t i = 0; i < slots.size(); ++i) {
    in[i] = slots[i] == UNUSED_INPUT ? zero : rails_of(slots[i]);  // unused reads 0
  }
  add_cube_cover(solver, next, cubes.ones, in, outr.h);
  add_cube_cover(solver, next, cubes.zeros, in, outr.l);
}

// A definite difference between the good and the faulty machine at one of the
// observed nets in the fault's out-cone: the good one known and the faulty one
// the other known value. Returns one literal per observed net (empty: none).
template <typename GoodOf, typename BadOf>
std::vector<int> add_definite_differences(CaDiCaL::Solver& solver, int& next,
                                          const ConeMasks& masks, const GoodOf& good_of,
                                          const BadOf& bad_of) {
  std::vector<int> differs;
  for (uint32_t obs : masks.observed) {
    if (masks.in_outcone[obs] == 0) {
      continue;
    }
    const Rails g = good_of(obs);
    const Rails f = bad_of(obs);
    const int d = next++;
    const int rise = next++;  // good 0, faulty 1
    const int fall = next++;  // good 1, faulty 0
    for (int lit : {g.l, -g.h, f.h, -f.l}) {
      add_clause(solver, {-rise, lit});
    }
    for (int lit : {g.h, -g.l, f.l, -f.h}) {
      add_clause(solver, {-fall, lit});
    }
    add_clause(solver, {-d, rise, fall});
    differs.push_back(d);
  }
  return differs;
}

}  // namespace

SatSolveResult solve_stuck_at_fault_x(const CompiledSimGraph& cg,
                                      const std::vector<AtpgPiInfo>& pis,
                                      const CompactFault& fault,
                                      const SatSolveOptions& options,
                                      std::map<std::string, bool>& out) {
  const std::vector<uint32_t> observable_set(cg.observable.begin(), cg.observable.end());
  const ConeMasks masks =
      build_cone_masks(cg, fault.net_index, observable_set, options.cone_restrict);
  if (masks.redundant) {
    return SatSolveResult::UNSAT;  // reaches no observable, whatever the unknowns
  }
  // A combinational loop in the cone is encoded as it is, cyclic equations any
  // consistent value satisfies -- not the X engine's fixed point. Its vectors
  // are candidates the X engine grades; an UNSAT through one proves nothing.
  bool unknown_in_cone = false;
  bool loop_in_cone = false;
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const SimNode& node = cg.nodes[i];
    if (masks.in_support[node.out] == 0 && masks.in_outcone[node.out] == 0) {
      continue;
    }
    unknown_in_cone = unknown_in_cone || is_x_source(node);
    if ((i < cg.in_loop.size() && cg.in_loop[i] != 0) || is_tristate(node.type)) {
      unknown_in_cone = true;
      loop_in_cone = true;  // no proof through a loop or a bus
    }
  }
  if (!unknown_in_cone) {
    return solve_stuck_at_fault(cg, pis, fault, options, out);
  }
  if (options.cnf_export != nullptr) {
    throw std::runtime_error("an X-mode graph's dual-rail SAT ATPG has no CNF export");
  }

  CaDiCaL::Solver solver;
  const auto time_guard = apply_solver_limits(solver, options);
  const size_t n = static_cast<size_t>(cg.net_count);
  int next = 1;
  const int truth = next++;
  add_unit(solver, truth);
  const Rails zero{-truth, truth};
  const Rails one{truth, -truth};
  const Rails unknown{truth, truth};

  // The good machine's rails, made on first use from the net's driver: a PI one
  // variable (h = v, l = ~v), a constant fixed, an unknown (1,1), a gate a pair.
  std::vector<Rails> good(n);
  std::vector<char> have_good(n, 0);
  const auto good_of = [&](uint32_t net) -> Rails {
    if (have_good[net] == 0) {
      have_good[net] = 1;
      const int d = cg.driver_index[net];
      const GateType type =
          d >= 0 ? cg.nodes[static_cast<size_t>(d)].type : GateType::INPUT;
      if (type == GateType::INPUT) {
        const int v = next++;
        good[net] = {v, -v};
      } else if (type == GateType::CONST0) {
        good[net] = zero;
      } else if (type == GateType::CONST1) {
        good[net] = one;
      } else if (type == GateType::CONSTX) {
        good[net] = unknown;
      } else {
        good[net].h = next++;
        good[net].l = next++;
      }
    }
    return good[net];
  };
  // The faulty machine's: the fault net forced, the rest of its out-cone a pair
  // of its own, everything else the good machine's -- and so is a source (an
  // input, a constant, an unknown), which no node of the faulty machine drives:
  // without cone_restrict every net counts as in the out-cone.
  std::vector<Rails> bad(n);
  std::vector<char> have_bad(n, 0);
  const auto bad_of = [&](uint32_t net) -> Rails {
    if (net == fault.net_index) {
      return fault.type == FaultType::SA1 ? one : zero;
    }
    if (masks.in_outcone[net] == 0) {
      return good_of(net);
    }
    const int d = cg.driver_index[net];
    const GateType source =
        d >= 0 ? cg.nodes[static_cast<size_t>(d)].type : GateType::INPUT;
    if (source == GateType::INPUT || source == GateType::CONST0 ||
        source == GateType::CONST1 || source == GateType::CONSTX) {
      return good_of(net);
    }
    if (have_bad[net] == 0) {
      have_bad[net] = 1;
      bad[net].h = next++;
      bad[net].l = next++;
    }
    return bad[net];
  };
  // One frame from power-up: a latch's held value unknown.
  const auto encode = [&](const SimNode& node, Rails outr, const auto& rails_of) {
    encode_dual_node(solver, next, cg, node, outr, rails_of, unknown, zero, one);
  };

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT || node.type == GateType::CONST0 ||
        node.type == GateType::CONST1 || node.type == GateType::CONSTX) {
      continue;
    }
    if (masks.in_support[node.out] != 0) {
      encode(node, good_of(node.out), good_of);
    }
    if (masks.in_outcone[node.out] != 0 && node.out != fault.net_index) {
      encode(node, bad_of(node.out), bad_of);
    }
  }

  std::vector<int> pi_literals;
  pi_literals.reserve(pis.size());
  for (const AtpgPiInfo& pi : pis) {
    pi_literals.push_back(good_of(pi.compiled).h);
  }
  for (const std::string& blocked : options.blocked_patterns) {
    if (blocked.size() != pis.size()) {
      throw std::runtime_error("blocked pattern length mismatch");
    }
    add_blocking_clause(solver, pi_literals, assignment_from_pattern(blocked));
  }
  // Scan compression: each seeded input's variable the XOR of its seed bits, as
  // in the two-valued CNF. Every input's variable exists by now, so the seed's
  // variables continue after them.
  CnfVarMap seed_vars(0);
  seed_vars.next = next;
  const NetVar seeded_var = [&](uint32_t net) {
    if (have_good[net] == 0) {
      throw std::runtime_error("dual-rail SAT ATPG: a seeded input is no input");
    }
    return good[net].h;
  };
  const std::vector<int> free_seed =
      add_seed_constraints(solver, seed_vars, options, seeded_var, nullptr);
  next = seed_vars.next;

  // A definite difference: the good machine known and the faulty one the
  // other known value, at some observed net.
  const std::vector<int> differs =
      add_definite_differences(solver, next, masks, good_of, bad_of);
  if (differs.empty()) {
    return SatSolveResult::UNSAT;
  }
  add_clause(solver, differs);

  const int status = solver.solve();
  const SatSolveResult mapped =
      map_cadical_result(status, status == 0 && had_solver_limits(options));
  if (mapped == SatSolveResult::SAT) {
    out.clear();
    for (const AtpgPiInfo& pi : pis) {
      out[pi.name] = solver.val(good_of(pi.compiled).h) > 0;
    }
    read_seed(solver, options, free_seed);
    return SatSolveResult::SAT;
  }
  if (mapped != SatSolveResult::UNSAT) {
    return mapped;
  }
  // No vector detects it definitely. The two-valued CNF says whether any shows
  // it for some value of the unknowns (POSSIBLE at best) or none can (redundant).
  const SatSolveResult two = solve_stuck_at_fault(cg, pis, fault, options, out);
  if (two == SatSolveResult::UNSAT && loop_in_cone) {
    return SatSolveResult::UNKNOWN;  // no proof through a loop: never redundant
  }
  return two == SatSolveResult::SAT ? SatSolveResult::POSSIBLE : two;
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
  if (options.cnf_export != nullptr) {
    throw std::runtime_error(
        "CNF export needs the single-shot solver: the incremental one builds "
        "its CNF in stages");
  }
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
  const auto time_guard = apply_solver_limits(solver, options);

  // Fault injection at the faulty site (permanent).
  add_unit(solver, (fault.type == FaultType::SA1)
                       ? vars.faulty_vars.at(fault_net)
                       : -vars.faulty_vars.at(fault_net));
  // The decompressor's seed (permanent): the PIs' good vars exist before their
  // cones are encoded.
  const std::vector<int> free_seed = add_seed_constraints(
      solver, vars, options,
      [&](uint32_t net) { return vars.free_vars.at(net); }, nullptr);

  // Blocked patterns over PI good vars (permanent; identical to the baseline).
  if (!options.blocked_patterns.empty()) {
    std::vector<int> pi_literals;
    pi_literals.reserve(pis.size());
    for (const AtpgPiInfo& pi : pis) {
      pi_literals.push_back(vars.free_vars.at(pi.compiled));
    }
    for (const std::string& blocked : options.blocked_patterns) {
      if (blocked.size() != pis.size()) {
        throw std::runtime_error(
            "blocked pattern length mismatch: pattern has " +
            std::to_string(blocked.size()) + " bits, expected " +
            std::to_string(pis.size()) +
            " PIs (C++/Python PI-list divergence -- check for an unmapped "
            "multi-bit-port bit)");
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
      // A PI, or an x/z bit of an X-mode graph (CONSTX: an input the test cannot
      // set, graded by the X engine): free, the same in both machines.
      if (node.type == GateType::INPUT || node.type == GateType::CONSTX) {
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
      read_seed(solver, options, free_seed);
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

namespace {

// The launch<->capture coupling of a two-frame transition CNF: each pair is
// (capture net, launch net), the capture-frame good value equal to the launch
// frame's. Broadside has none (launch and capture PIs independent); scan LOC
// couples each capture PPI to its launch PPO, scan LOS to its chain
// predecessor's launch PPI, and both hold the real PIs.
using Coupling = std::vector<std::pair<uint32_t, uint32_t>>;

// The launch-frame nets the CNF reads (lazy launch frame): the fault net, for
// its pre-transition value, and the launch net of every coupling whose capture
// net the capture machines read (in the capture support), each with its fan-in
// cone. No other clause names a launch net, so the gates left out only define
// values nothing constrains; any model of the smaller CNF extends to the whole
// two-frame CNF by simulating them, and the verdict is the same.
std::vector<char> launch_cone(const CompiledSimGraph& cg, uint32_t fault_net,
                              const Coupling& coupling, const ConeMasks& masks) {
  std::vector<char> needed(static_cast<size_t>(cg.net_count), 0);
  std::vector<uint32_t> work;
  const auto need = [&](uint32_t net) {
    if (!needed[net]) {
      needed[net] = 1;
      work.push_back(net);
    }
  };
  need(fault_net);
  for (const auto& [capture, launch] : coupling) {
    if (masks.in_support[capture]) {
      need(launch);
    }
  }
  // An X-mode graph's latch the capture machines read holds its launch-frame
  // value there.
  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::LATCH &&
        (masks.in_support[node.out] || masks.in_outcone[node.out])) {
      need(node.out);
    }
  }
  std::vector<int> scratch;
  close_fanin(cg, driver_index_of(cg, scratch), needed, work);
  return needed;
}

// The fault-free launch frame over the fan-in cone of `nets`, for the
// launch-net values `launch_value` gives the INPUT nets (the solved launch):
// the cone's nets, in level order; every other net false.
std::vector<bool> simulate_launch(const CompiledSimGraph& cg,
                                  std::vector<uint32_t> nets,
                                  const std::function<bool(uint32_t)>& launch_value) {
  std::vector<char> cone(static_cast<size_t>(cg.net_count), 0);
  for (uint32_t net : nets) {
    cone[net] = 1;
  }
  std::vector<int> scratch;
  close_fanin(cg, driver_index_of(cg, scratch), cone, nets);
  std::vector<bool> values(static_cast<size_t>(cg.net_count), false);
  std::vector<bool> ins;
  const auto eval_node = [&](const SimNode& node) {
    if (!cone[node.out]) {
      return;
    }
    if (node.type == GateType::INPUT) {
      values[node.out] = launch_value(node.out);
      return;
    }
    ins.clear();
    for (uint32_t input : {node.in0, node.in1, node.in2, node.in3, node.in4,
                           node.in5}) {
      if (input != UNUSED_INPUT) {
        ins.push_back(values[input]);
      }
    }
    values[node.out] = eval_gate_scalar(node.type, ins);
  };
  if (cg.level_starts.size() >= 2) {
    for (size_t lvl = 0; lvl + 1 < cg.level_starts.size(); ++lvl) {
      for (int i = cg.level_starts[lvl]; i < cg.level_starts[lvl + 1]; ++i) {
        eval_node(cg.nodes[static_cast<size_t>(i)]);
      }
    }
  } else {
    for (const SimNode& node : cg.nodes) {
      eval_node(node);
    }
  }
  return values;
}

// Shared two-frame transition CNF: three net-variable copies (launch good,
// capture good, capture faulty), the transition edge requirement + capture
// stuck-at force at the fault net, and the capture good-vs-faulty miter over the
// observable set. The only thing that varies between broadside and scan LOC/LOS
// is `coupling`; everything else is identical.
SatSolveResult solve_two_frame_transition(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const CompactFault& fault, const SatSolveOptions& options,
    const Coupling& coupling, bool launch_only_blocking,
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
  const auto time_guard = apply_solver_limits(solver, options);
  std::vector<std::vector<int>> clauses;
  const ClauseRecorder recorder(options.cnf_export != nullptr ? &clauses
                                                              : nullptr);

  // Cone masks restrict the two CAPTURE-frame machines (good + faulty); the
  // LAUNCH frame is encoded over launch_cone only. Empty reached-observable
  // set => redundant.
  const std::vector<uint32_t> observable_set(cg.observable.begin(),
                                             cg.observable.end());
  const ConeMasks masks = build_cone_masks(cg, fault.net_index, observable_set,
                                           options.cone_restrict);
  if (masks.redundant) {
    export_structural_unsat(options);
    return SatSolveResult::UNSAT;
  }
  const std::vector<char> in_launch =
      options.lazy_launch
          ? launch_cone(cg, fault.net_index, coupling, masks)
          : std::vector<char>(static_cast<size_t>(cg.net_count), 1);

  int latch_truth = 0;  // a literal fixed true, made for the first latch
  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT ||
        !(in_launch[node.out] || masks.in_support[node.out] ||
          masks.in_outcone[node.out])) {
      continue;
    }
    // An X-mode graph's nodes, as solve_stuck_at_fault reads them (a two-valued
    // graph has none): an x/z bit free in each frame, the capture machines
    // sharing it; a bus free everywhere (no proof through it).
    if (node.type == GateType::CONSTX) {
      if (masks.in_outcone[node.out] && node.out != fault.net_index) {
        add_equiv(solver, vars.free_vars.at(node.out), vars.faulty_vars.at(node.out));
      }
      continue;
    }
    if (is_tristate(node.type)) {
      continue;
    }
    if (node.type == GateType::LATCH) {
      // Its power-up value a free variable in the launch frame; in the capture
      // frame both machines hold its launch-frame value (the launch is
      // fault-free).
      if (latch_truth == 0) {
        latch_truth = vars.next++;
        add_unit(solver, latch_truth);
      }
      const CompiledFFConfig& lcfg = cg.latch_configs.at(node.ff_cfg);
      const std::array<uint32_t, 4> slots{node.in0, node.in1, node.in2, node.in3};
      const auto pins = [&](const auto& lit_of) {
        std::array<int, 4> lits{};
        for (size_t i = 0; i < slots.size(); ++i) {
          lits[i] = slots[i] == UNUSED_INPUT ? 0 : lit_of(slots[i]);
        }
        return lits;
      };
      const int held = launch_vars.at(node.out);
      if (in_launch[node.out]) {
        const auto p = pins([&](uint32_t net) { return launch_vars.at(net); });
        const int state = vars.next++;
        add_latch_cnf(solver, vars.next, latch_truth, lcfg, p[0], p[1], p[2], p[3], state,
                      held);
      }
      if (masks.in_support[node.out]) {
        const auto p = pins([&](uint32_t net) { return vars.free_vars.at(net); });
        add_latch_cnf(solver, vars.next, latch_truth, lcfg, p[0], p[1], p[2], p[3], held,
                      vars.free_vars.at(node.out));
      }
      if (masks.in_outcone[node.out] && node.out != fault.net_index) {
        const auto p =
            pins([&](uint32_t net) { return faulty_input_lit(vars, masks, net); });
        add_latch_cnf(solver, vars.next, latch_truth, lcfg, p[0], p[1], p[2], p[3], held,
                      vars.faulty_vars.at(node.out));
      }
      continue;
    }
    const auto input_nets = inputs_of(node);
    // Launch frame: the good machine (no fault anywhere) over launch_cone.
    if (in_launch[node.out]) {
      std::vector<int> launch_inputs;
      launch_inputs.reserve(input_nets.size());
      for (uint32_t input : input_nets) {
        launch_inputs.push_back(launch_vars.at(input));
      }
      add_gate_cnf(solver, node.type, launch_inputs, launch_vars.at(node.out));
    }
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

  // Launch<->capture coupling: broadside = none (independent); scan LOC =
  // capture PPI == launch PPO + held real PIs. Installed on the GOOD capture
  // machine even when the capture net is the fault net (the faulty copy is
  // independently forced to the stuck value).
  for (const auto& [capture, launch] : coupling) {
    add_equiv(solver, vars.free_vars.at(capture), launch_vars.at(launch));
  }
  // Scan compression: the launch frame's load and (LOS) the capture frame's
  // launch-shift scan-in bits come from one seed.
  const NetVar capture_var = [&](uint32_t net) { return vars.free_vars.at(net); };
  const std::vector<int> free_seed = add_seed_constraints(
      solver, vars, options,
      [&](uint32_t net) { return launch_vars.at(net); }, &capture_var);

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
      throw std::runtime_error(
          "blocked pattern length mismatch: pattern has " +
          std::to_string(blocked.size()) + " bits, expected " +
          std::to_string(block_literals.size()) +
          " PI/PPI literals (C++/Python PI-list divergence -- check for an "
          "unmapped multi-bit-port bit)");
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
  export_cnf(
      clauses, options, vars.next - 1, pis,
      [&](uint32_t net) { return vars.free_vars.at(net); },
      [&](uint32_t net) { return launch_vars.at(net); },
      vars.faulty_vars.at(fault.net_index), diff_vars);

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
  // A coupled capture value the CNF left free (its launch net outside
  // launch_cone): what the launch frame computes from V1, fault-free. Not on an
  // X-mode graph, whose launch frame may leave it unknown (and holds nodes a
  // scalar evaluation has no value for): the scan flows derive the capture from
  // the launch themselves.
  std::map<uint32_t, uint32_t> derived;  // capture net -> launch net
  for (const auto& [capture, launch] : coupling) {
    if (!in_launch[launch] && !cg.x_mode) {
      derived[capture] = launch;
    }
  }
  if (!derived.empty()) {
    std::vector<uint32_t> launch_nets;
    for (const auto& [capture, launch] : derived) {
      launch_nets.push_back(launch);
    }
    const std::vector<bool> launch_frame = simulate_launch(
        cg, std::move(launch_nets),
        [&](uint32_t net) { return solver.val(launch_vars.at(net)) > 0; });
    for (const AtpgPiInfo& pi : pis) {
      const auto it = derived.find(pi.compiled);
      if (it != derived.end()) {
        v2_out[pi.name] = launch_frame[it->second];
      }
    }
  }
  read_seed(solver, options, free_seed);
  return SatSolveResult::SAT;
}

// solve_two_frame_transition on an X-mode graph: solve_stuck_at_fault_x's
// dual-rail CNF in two frames. Three rail copies -- the launch frame's good
// machine, the capture frame's good and faulty ones; the edge required known at
// the fault net (pre in the launch frame, post in the capture's good machine); a
// definite difference at an observed net. An unknown is (1,1) in each frame; a
// latch powers up unknown in the launch frame and holds its launch-frame value in
// the capture frame, as the X engine evaluates it. A coupled capture net reads
// its launch net's rails. With no unknown, loop or bus in a cone this is the
// two-valued solver. No definite pair: the two-valued CNF, every unknown free,
// says POSSIBLE (SAT, its pair) or redundant (UNSAT; UNKNOWN through a loop or a
// bus, which the CNF does not model).
SatSolveResult solve_two_frame_transition_x(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const CompactFault& fault, const SatSolveOptions& options,
    const Coupling& coupling, bool launch_only_blocking,
    const std::vector<uint32_t>& extra_block_compiled,
    std::map<std::string, bool>& v1_out, std::map<std::string, bool>& v2_out) {
  const auto two_valued = [&]() {
    return solve_two_frame_transition(cg, pis, fault, options, coupling,
                                      launch_only_blocking, extra_block_compiled, v1_out,
                                      v2_out);
  };
  const std::vector<uint32_t> observable_set(cg.observable.begin(), cg.observable.end());
  const ConeMasks masks =
      build_cone_masks(cg, fault.net_index, observable_set, options.cone_restrict);
  if (masks.redundant) {
    return two_valued();  // reaches no observable, whatever the unknowns
  }
  const size_t n = static_cast<size_t>(cg.net_count);
  // The launch frame over the fault net, the coupled launch nets the capture
  // machines read and every latch they read (launch_cone). A coupled capture
  // value outside it is left free: the scan flows derive the capture from the
  // launch themselves, and LOS reads only its uncoupled head bits.
  const std::vector<char> in_launch = launch_cone(cg, fault.net_index, coupling, masks);
  bool unknown_in_cone = false;
  bool loop_in_cone = false;
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const SimNode& node = cg.nodes[i];
    if (in_launch[node.out] == 0 && masks.in_support[node.out] == 0 &&
        masks.in_outcone[node.out] == 0) {
      continue;
    }
    unknown_in_cone = unknown_in_cone || is_x_source(node);
    if ((i < cg.in_loop.size() && cg.in_loop[i] != 0) || is_tristate(node.type)) {
      unknown_in_cone = true;
      loop_in_cone = true;
    }
  }
  if (!unknown_in_cone) {
    return two_valued();
  }
  if (options.cnf_export != nullptr) {
    throw std::runtime_error("an X-mode graph's dual-rail SAT ATPG has no CNF export");
  }

  CaDiCaL::Solver solver;
  const auto time_guard = apply_solver_limits(solver, options);
  int next = 1;
  const int truth = next++;
  add_unit(solver, truth);
  const Rails zero{-truth, truth};
  const Rails one{truth, -truth};
  const Rails unknown{truth, truth};

  // A machine's rails, made on first use from the net's driver: a PI one
  // variable (h = v, l = ~v), a constant fixed, an unknown (1,1), a gate a pair.
  struct Copy {
    std::vector<Rails> rails;
    std::vector<char> have;
  };
  Copy launch{std::vector<Rails>(n), std::vector<char>(n, 0)};
  Copy good = launch;
  Copy bad = launch;
  const auto make = [&](Copy& copy, uint32_t net) -> Rails {
    if (copy.have[net] == 0) {
      copy.have[net] = 1;
      const int d = cg.driver_index[net];
      const GateType type =
          d >= 0 ? cg.nodes[static_cast<size_t>(d)].type : GateType::INPUT;
      if (type == GateType::INPUT) {
        const int v = next++;
        copy.rails[net] = {v, -v};
      } else if (type == GateType::CONST0) {
        copy.rails[net] = zero;
      } else if (type == GateType::CONST1) {
        copy.rails[net] = one;
      } else if (type == GateType::CONSTX) {
        copy.rails[net] = unknown;
      } else {
        copy.rails[net].h = next++;
        copy.rails[net].l = next++;
      }
    }
    return copy.rails[net];
  };
  std::map<uint32_t, uint32_t> coupled;  // capture net -> launch net
  for (const auto& [capture, launch_net] : coupling) {
    coupled[capture] = launch_net;
  }
  const auto launch_of = [&](uint32_t net) { return make(launch, net); };
  const auto good_of = [&](uint32_t net) -> Rails {
    const auto it = coupled.find(net);
    return it != coupled.end() ? launch_of(it->second) : make(good, net);
  };
  // The faulty machine: the fault net forced, the rest of its out-cone its own
  // rails, every other net -- and every source -- the good machine's.
  const auto bad_of = [&](uint32_t net) -> Rails {
    if (net == fault.net_index) {
      return fault.type == FaultType::SA1 ? one : zero;
    }
    if (masks.in_outcone[net] == 0) {
      return good_of(net);
    }
    const int d = cg.driver_index[net];
    const GateType source =
        d >= 0 ? cg.nodes[static_cast<size_t>(d)].type : GateType::INPUT;
    if (source == GateType::INPUT || source == GateType::CONST0 ||
        source == GateType::CONST1 || source == GateType::CONSTX) {
      return good_of(net);
    }
    return make(bad, net);
  };

  for (const SimNode& node : cg.nodes) {
    if (node.type == GateType::INPUT || node.type == GateType::CONST0 ||
        node.type == GateType::CONST1 || node.type == GateType::CONSTX) {
      continue;
    }
    if (in_launch[node.out] != 0) {
      encode_dual_node(solver, next, cg, node, launch_of(node.out), launch_of, unknown,
                       zero, one);
    }
    const Rails held = (node.type == GateType::LATCH && in_launch[node.out] != 0)
                           ? launch_of(node.out)
                           : unknown;
    if (masks.in_support[node.out] != 0) {
      encode_dual_node(solver, next, cg, node, good_of(node.out), good_of, held, zero,
                       one);
    }
    if (masks.in_outcone[node.out] != 0 && node.out != fault.net_index) {
      encode_dual_node(solver, next, cg, node, bad_of(node.out), bad_of, held, zero,
                       one);
    }
  }

  // The edge: the good machine known at the pre value in the launch frame and at
  // the post value in the capture frame.
  const auto require = [&](Rails r, Logic v) {
    add_unit(solver, v == Logic::ONE ? r.h : -r.h);
    add_unit(solver, v == Logic::ONE ? -r.l : r.l);
  };
  const Logic pre = fault.type == FaultType::SA0 ? Logic::ZERO : Logic::ONE;
  require(launch_of(fault.net_index), pre);
  require(good_of(fault.net_index), pre == Logic::ZERO ? Logic::ONE : Logic::ZERO);

  // Blocked patterns over the same literals as the two-valued solver's.
  std::vector<int> block_literals;
  std::vector<int> capture_literals;
  for (const AtpgPiInfo& pi : pis) {
    block_literals.push_back(launch_of(pi.compiled).h);
    capture_literals.push_back(good_of(pi.compiled).h);
  }
  if (!launch_only_blocking) {
    block_literals.insert(block_literals.end(), capture_literals.begin(),
                          capture_literals.end());
  } else {
    for (uint32_t idx : extra_block_compiled) {
      block_literals.push_back(good_of(idx).h);
    }
  }
  for (const std::string& blocked : options.blocked_patterns) {
    if (blocked.size() != block_literals.size()) {
      throw std::runtime_error("blocked pattern length mismatch");
    }
    add_blocking_clause(solver, block_literals, assignment_from_pattern(blocked));
  }
  // Scan compression: the launch load and (LOS) the capture's launch-shift bits
  // from one seed, as in the two-valued CNF; every input's rails exist by now.
  CnfVarMap seed_vars(0);
  seed_vars.next = next;
  const NetVar seeded_launch = [&](uint32_t net) {
    if (launch.have[net] == 0) {
      throw std::runtime_error("dual-rail SAT ATPG: a seeded input is no input");
    }
    return launch.rails[net].h;
  };
  const NetVar seeded_capture = [&](uint32_t net) {
    if (coupled.count(net) == 0 && good.have[net] == 0) {
      throw std::runtime_error("dual-rail SAT ATPG: a seeded input is no input");
    }
    return good_of(net).h;
  };
  const std::vector<int> free_seed =
      add_seed_constraints(solver, seed_vars, options, seeded_launch, &seeded_capture);
  next = seed_vars.next;

  const std::vector<int> differs =
      add_definite_differences(solver, next, masks, good_of, bad_of);
  if (differs.empty()) {
    return two_valued();
  }
  add_clause(solver, differs);

  const int status = solver.solve();
  const SatSolveResult mapped =
      map_cadical_result(status, status == 0 && had_solver_limits(options));
  if (mapped == SatSolveResult::SAT) {
    v1_out.clear();
    v2_out.clear();
    for (const AtpgPiInfo& pi : pis) {
      v1_out[pi.name] = solver.val(launch_of(pi.compiled).h) > 0;
      v2_out[pi.name] = solver.val(good_of(pi.compiled).h) > 0;
    }
    read_seed(solver, options, free_seed);
    return SatSolveResult::SAT;
  }
  if (mapped != SatSolveResult::UNSAT) {
    return mapped;
  }
  // No pair detects it definitely. The two-valued CNF says whether any shows it
  // for some value of the unknowns (POSSIBLE at best) or none can (redundant).
  const SatSolveResult two = two_valued();
  if (two == SatSolveResult::UNSAT && loop_in_cone) {
    return SatSolveResult::UNKNOWN;  // no proof through a loop or a bus
  }
  return two == SatSolveResult::SAT ? SatSolveResult::POSSIBLE : two;
}

}  // namespace

SatSolveResult solve_transition_fault_x(const CompiledSimGraph& cg,
                                        const std::vector<AtpgPiInfo>& pis,
                                        const CompactFault& fault,
                                        const SatSolveOptions& options,
                                        std::map<std::string, bool>& v1_out,
                                        std::map<std::string, bool>& v2_out) {
  return solve_two_frame_transition_x(cg, pis, fault, options, Coupling{},
                                      /*launch_only_blocking=*/false,
                                      /*extra_block_compiled=*/{}, v1_out, v2_out);
}

SatSolveResult solve_scan_transition_fault_x(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LocCouple>& couples,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out) {
  Coupling loc_coupling;
  loc_coupling.reserve(couples.size() + held_pi_compiled.size());
  for (const LocCouple& c : couples) {
    loc_coupling.emplace_back(c.ppi_compiled, c.ppo_compiled);
  }
  for (uint32_t held : held_pi_compiled) {
    loc_coupling.emplace_back(held, held);
  }
  return solve_two_frame_transition_x(cg, pis, fault, options, loc_coupling,
                                      /*launch_only_blocking=*/true,
                                      /*extra_block_compiled=*/{}, v1_out, v2_out);
}

SatSolveResult solve_scan_los_transition_fault_x(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LosCouple>& couples,
    const std::vector<uint32_t>& head_ppi_compiled,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out) {
  Coupling los_coupling;
  los_coupling.reserve(couples.size() + held_pi_compiled.size());
  for (const LosCouple& c : couples) {
    los_coupling.emplace_back(c.capture_ppi_compiled, c.pred_ppi_compiled);
  }
  for (uint32_t held : held_pi_compiled) {
    los_coupling.emplace_back(held, held);
  }
  return solve_two_frame_transition_x(cg, pis, fault, options, los_coupling,
                                      /*launch_only_blocking=*/true, head_ppi_compiled,
                                      v1_out, v2_out);
}

SatSolveResult solve_transition_fault(const CompiledSimGraph& cg,
                                      const std::vector<AtpgPiInfo>& pis,
                                      const CompactFault& fault,
                                      const SatSolveOptions& options,
                                      std::map<std::string, bool>& v1_out,
                                      std::map<std::string, bool>& v2_out) {
  // Broadside two-pattern: launch and capture PIs are independent, so no extra
  // coupling beyond the shared good==faulty capture equivs.
  return solve_two_frame_transition(cg, pis, fault, options, Coupling{},
                                    /*launch_only_blocking=*/false,
                                    /*extra_block_compiled=*/{}, v1_out, v2_out);
}

SatSolveResult solve_scan_transition_fault(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LocCouple>& couples,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out) {
  // Per scan FF: capture-frame current state (PPI) == launch-frame next-state
  // (PPO), the LOC functional-launch derivation V2 = f(V1); real PIs hold
  // launch->capture (single-clock LOC convention).
  Coupling loc_coupling;
  loc_coupling.reserve(couples.size() + held_pi_compiled.size());
  for (const LocCouple& c : couples) {
    loc_coupling.emplace_back(c.ppi_compiled, c.ppo_compiled);
  }
  for (uint32_t held : held_pi_compiled) {
    loc_coupling.emplace_back(held, held);
  }
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
  // Per scan FF: capture-frame current state (PPI) == chain PREDECESSOR's
  // launch-frame current state (PPI), the shift relation V2[ff] =
  // V1[predecessor(ff)] created by the last scan-shift launch. Chain HEAD PPIs
  // get NO pair: their capture value is the fresh launch scan-in bit (a free
  // variable). Real PIs hold launch->capture (one functional capture clock
  // after the shift).
  Coupling los_coupling;
  los_coupling.reserve(couples.size() + held_pi_compiled.size());
  for (const LosCouple& c : couples) {
    los_coupling.emplace_back(c.capture_ppi_compiled, c.pred_ppi_compiled);
  }
  for (uint32_t held : held_pi_compiled) {
    los_coupling.emplace_back(held, held);
  }
  // V2 = shift(V1) plus the free head scan-in bits, so the blocked key is
  // launch ‖ head-bits (launch_only_blocking with the head PPIs appended).
  return solve_two_frame_transition(cg, pis, fault, options, los_coupling,
                                    /*launch_only_blocking=*/true,
                                    head_ppi_compiled, v1_out, v2_out);
}

}  // namespace faultflow::atpg
