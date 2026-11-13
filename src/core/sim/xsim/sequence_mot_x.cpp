#include "sim/xsim/sequence_mot_x.hpp"

#include <cadical.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <map>
#include <optional>
#include <stdexcept>
#include <unordered_map>

#include "atpg/sat_terminator.hpp"
#include "common/threads.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "sim/gate_eval.hpp"

namespace faultflow {
namespace {

// A net's value in the symbolic run, as an AIG literal: 0 false, 1 true, 2v + n
// SAT variable v (>= 1), negated when n is 1.
using Lit = int32_t;
constexpr Lit kFalse = 0;
constexpr Lit kTrue = 1;
inline bool is_const(Lit a) { return a < 2; }
inline Lit lit_not(Lit a) { return a ^ 1; }
inline Lit lit_of(bool v) { return v ? kTrue : kFalse; }
inline int var_of(Lit a) { return a >> 1; }
inline int cadical_lit(Lit a) { return (a & 1) != 0 ? -var_of(a) : var_of(a); }

// A fault's run went past its variable or time budget.
struct OutOfBudget {};

// The table of sel ? if1 : if0 over inputs (sel, if0, if1).
constexpr uint64_t mux_table() {
  uint64_t t = 0;
  for (uint32_t a = 0; a < 8; ++a) {
    const bool sel = (a & 1u) != 0;
    const bool v = sel ? (a & 4u) != 0 : (a & 2u) != 0;
    t |= static_cast<uint64_t>(v) << a;
  }
  return t;
}
constexpr uint64_t kAndTable = 0b1000;
constexpr uint64_t kMuxTable = mux_table();

inline uint64_t table_mask(int k) {
  return k == 6 ? ~uint64_t{0} : (uint64_t{1} << (1u << k)) - 1;
}

// Does a k-input table depend on input p?
bool depends(uint64_t table, int k, int p) {
  for (uint32_t b = 0; b < (1u << k); ++b) {
    if ((b & (1u << p)) == 0 &&
        ((table >> b) & 1) != ((table >> (b | (1u << p))) & 1)) {
      return true;
    }
  }
  return false;
}

// The table without input p (which it doesn't depend on).
uint64_t drop_input(uint64_t table, int k, int p) {
  uint64_t out = 0;
  for (uint32_t nb = 0; nb < (1u << (k - 1)); ++nb) {
    const uint32_t low = nb & ((1u << p) - 1);
    const uint32_t b = low | ((nb >> p) << (p + 1));
    out |= ((table >> b) & 1) << nb;
  }
  return out;
}

struct TableKey {
  uint64_t table = 0;
  std::array<int32_t, 6> vars{};
  uint8_t count = 0;
  bool operator==(const TableKey& o) const {
    return table == o.table && count == o.count && vars == o.vars;
  }
};

struct TableKeyHash {
  size_t operator()(const TableKey& k) const {
    uint64_t h = k.table * 0x9E3779B97F4A7C15ULL + k.count;
    for (int32_t v : k.vars) {
      h = (h ^ static_cast<uint64_t>(v)) * 0x100000001B3ULL;
    }
    return static_cast<size_t>(h ^ (h >> 29));
  }
};

// Literals over a CaDiCaL solver: every function a canonical table over its
// distinct, ascending, relevant variables (constants folded, negated inputs and
// a true-at-zero output folded into the table), structurally hashed, and Tseitin
// encoded once by its full table.
class Symbolic {
 public:
  Symbolic(CaDiCaL::Solver& solver, int64_t max_variables)
      : solver_(solver),
        max_variables_(std::min<int64_t>(max_variables, (int64_t{1} << 30) - 1)) {}

  Lit fresh() {
    if (variables_ >= max_variables_) {
      throw OutOfBudget{};
    }
    ++variables_;
    return static_cast<Lit>(variables_ << 1);
  }

  // A function of n <= 6 literals by its table (bit a: the output at input
  // assignment a, input i at bit i).
  Lit apply(uint64_t table, int n, const Lit* in) {
    uint32_t index = 0;
    bool constant = true;
    for (int i = 0; i < n; ++i) {
      if (is_const(in[i])) {
        index |= static_cast<uint32_t>(in[i]) << i;
      } else {
        constant = false;
      }
    }
    if (constant) {
      return lit_of(((table >> index) & 1) != 0);
    }
    TableKey key;
    int k = 0;
    for (int i = 0; i < n; ++i) {
      if (!is_const(in[i]) &&
          std::find(key.vars.begin(), key.vars.begin() + k, var_of(in[i])) ==
              key.vars.begin() + k) {
        key.vars[static_cast<size_t>(k++)] = var_of(in[i]);
      }
    }
    std::sort(key.vars.begin(), key.vars.begin() + k);
    std::array<int, 6> position{};
    for (int i = 0; i < n; ++i) {
      if (!is_const(in[i])) {
        position[static_cast<size_t>(i)] = static_cast<int>(
            std::find(key.vars.begin(), key.vars.begin() + k, var_of(in[i])) -
            key.vars.begin());
      }
    }
    uint64_t reduced = 0;
    for (uint32_t b = 0; b < (1u << k); ++b) {
      uint32_t a = 0;
      for (int i = 0; i < n; ++i) {
        const uint32_t bit =
            is_const(in[i])
                ? static_cast<uint32_t>(in[i])
                : (((b >> position[static_cast<size_t>(i)]) & 1u) ^
                   static_cast<uint32_t>(in[i] & 1));
        a |= bit << i;
      }
      reduced |= ((table >> a) & 1) << b;
    }
    for (int p = k - 1; p >= 0; --p) {
      if (!depends(reduced, k, p)) {
        reduced = drop_input(reduced, k, p);
        std::copy(key.vars.begin() + p + 1, key.vars.begin() + k, key.vars.begin() + p);
        --k;
        key.vars[static_cast<size_t>(k)] = 0;
      }
    }
    if (k == 0) {
      return lit_of((reduced & 1) != 0);
    }
    const Lit negate = static_cast<Lit>(reduced & 1);
    reduced = negate != 0 ? ~reduced & table_mask(k) : reduced;
    if (k == 1) {  // the identity: its bit 0 is 0 and it depends on the input
      return (key.vars[0] << 1) ^ negate;
    }
    key.table = reduced;
    key.count = static_cast<uint8_t>(k);
    const auto found = cache_.find(key);
    if (found != cache_.end()) {
      return found->second ^ negate;
    }
    const Lit out = fresh();
    for (uint32_t b = 0; b < (1u << k); ++b) {
      for (int p = 0; p < k; ++p) {
        const int v = key.vars[static_cast<size_t>(p)];
        solver_.add(((b >> p) & 1u) != 0 ? -v : v);
      }
      solver_.add(((reduced >> b) & 1) != 0 ? var_of(out) : -var_of(out));
      solver_.add(0);
    }
    cache_.emplace(key, out);
    return out ^ negate;
  }

  Lit and_(Lit a, Lit b) {
    if (a == kFalse || b == kFalse || a == lit_not(b)) {
      return kFalse;
    }
    if (a == kTrue || a == b) {
      return b;
    }
    if (b == kTrue) {
      return a;
    }
    const Lit in[2] = {a, b};
    return apply(kAndTable, 2, in);
  }

  Lit or_(Lit a, Lit b) { return lit_not(and_(lit_not(a), lit_not(b))); }

  // sel ? if1 : if0.
  Lit mux(Lit sel, Lit if0, Lit if1) {
    if (sel == kFalse || if0 == if1) {
      return if0;
    }
    if (sel == kTrue) {
      return if1;
    }
    const Lit in[3] = {sel, if0, if1};
    return apply(kMuxTable, 3, in);
  }

 private:
  CaDiCaL::Solver& solver_;
  int64_t max_variables_;
  int64_t variables_ = 0;
  std::unordered_map<TableKey, Lit, TableKeyHash> cache_;
};

// What every fault's run shares: the graph, its gates' tables, the sequence with
// each cycle's input values and unknowns by pi_nets entry, the good machine's
// observed values and the bits compared.
struct Plan {
  const CompiledSimGraph* cg = nullptr;
  TestVector vec;
  XRunOptions options;
  int eval_begin = 0;
  int eval_end = 0;
  std::vector<uint64_t> table;                  // per node, a gate's
  std::vector<uint8_t> arity;                   // per node, a gate's used inputs
  std::vector<std::array<uint32_t, 6>> inputs;  // per node, those inputs in order
  std::vector<std::vector<uint8_t>> pi_value;   // per cycle, per pi_nets entry
  std::vector<std::vector<uint8_t>> pi_unknown;
  std::vector<uint32_t> observe;
  std::vector<std::vector<Logic>> golden;
  std::vector<std::vector<bool>> compare;  // per sample, per observed output
  int last_cycle = -1;                     // the last cycle with a compared bit
};

bool unsupported_node(GateType type) {
  return type == GateType::TBUF_P || type == GateType::TBUF_N ||
         type == GateType::TINV_P || type == GateType::TINV_N ||
         type == GateType::BUS_MERGE || type == GateType::BUS_RES;
}

bool plain_gate(GateType type) {
  return type != GateType::INPUT && type != GateType::DFF && type != GateType::CONST0 &&
         type != GateType::CONST1 && type != GateType::CONSTX &&
         type != GateType::LATCH && !is_icg(type) && !unsupported_node(type);
}

// A constraint: at sample `sample`, the faulty machine's literal must take the
// good machine's value for the chip to escape.
struct Match {
  int32_t sample;
  int lit;
};

struct Decision {
  MotOutcome outcome = MotOutcome::UNDECIDED;
  int32_t by = -1;
  std::vector<int8_t> witness;
};

// The faulty machine, run on literals: GoldenRefSimX::simulate_sequence step for
// step, every X it would see a fresh variable (power-up values, unknown inputs,
// an x bit at each evaluation), every decision an exact mux.
class FaultyRun {
 public:
  FaultyRun(const Plan& plan, const CompactFault& fault, Symbolic& sym,
            std::optional<std::chrono::steady_clock::time_point> deadline)
      : plan_(plan),
        cg_(*plan.cg),
        fault_(fault),
        sym_(sym),
        deadline_(deadline),
        values_(static_cast<size_t>(cg_.net_count), kFalse),
        prev_(static_cast<size_t>(cg_.net_count), kFalse),
        latches_(static_cast<size_t>(cg_.net_count), kFalse),
        memo_in_(cg_.nodes.size()),
        memo_out_(cg_.nodes.size(), -1) {}

  // Runs to the last compared sample (or the first constant mismatch), filling
  // `matches`, the earliest mismatch sample `conflict` (-1: none) and each
  // flop's power-up literal.
  void run(std::vector<Match>& matches, int32_t& conflict, std::vector<Lit>& power_up) {
    const bool unknown = plan_.options.initial_state == Logic::X;
    std::vector<Lit> ff_states(cg_.ff_configs.size(), kFalse);
    for (Lit& state : ff_states) {
      state = unknown ? sym_.fresh() : kFalse;
    }
    for (const auto& [idx, value] : plan_.vec.initial_ff_state) {
      if (idx < ff_states.size()) {
        ff_states[idx] = lit_of(value);
      }
    }
    power_up = ff_states;
    for (const SimNode& sn : cg_.nodes) {
      if (unknown && (sn.type == GateType::LATCH || is_icg(sn.type))) {
        latches_[sn.out] = sym_.fresh();
      }
    }
    bool settled = false;
    int32_t sample = 0;
    conflict = -1;
    for (int c = 0; c <= plan_.last_cycle; ++c) {
      if (deadline_ && std::chrono::steady_clock::now() >= *deadline_) {
        throw OutOfBudget{};
      }
      const TestCycle& cycle = plan_.vec.cycles[static_cast<size_t>(c)];
      active_ = cycle.fault_active;
      for (size_t j = 0; j < cg_.pi_nets.size(); ++j) {
        const auto pi = static_cast<uint32_t>(cg_.pi_nets[j]);
        values_[pi] = plan_.pi_unknown[static_cast<size_t>(c)][j] != 0
                          ? sym_.fresh()
                          : lit_of(plan_.pi_value[static_cast<size_t>(c)][j] != 0);
        force(pi);
      }
      seed(ff_states);
      evaluate();
      update_ff_states(ff_states, settled);
      seed(ff_states);
      evaluate();
      for (int i = 0; i < cycle.settle_cycles; ++i) {
        seed(ff_states);
        evaluate();
      }
      if (cycle.sample_outputs) {
        const auto& golden = plan_.golden[static_cast<size_t>(sample)];
        const auto& compare = plan_.compare[static_cast<size_t>(sample)];
        for (size_t o = 0; o < plan_.observe.size(); ++o) {
          if (!compare[o] || !is_known(golden[o])) {
            continue;
          }
          const Lit v = values_[plan_.observe[o]];
          const bool good = golden[o] == Logic::ONE;
          if (is_const(v)) {
            if (v != lit_of(good)) {
              conflict = sample;
            }
          } else {
            matches.push_back({sample, cadical_lit(good ? v : lit_not(v))});
          }
        }
        if (conflict >= 0) {
          return;
        }
        ++sample;
      }
      prev_ = values_;
      settled = true;
    }
  }

 private:
  void force(uint32_t net) {
    if (active_ && fault_.net_index == net) {
      values_[net] = lit_of(fault_.type != FaultType::SA0);
    }
  }

  void seed(const std::vector<Lit>& ff_states) {
    for (size_t idx = 0; idx < cg_.ff_nodes.size(); ++idx) {
      const SimNode& sn = cg_.nodes[static_cast<size_t>(cg_.ff_nodes[idx])];
      values_[sn.out] = ff_states[idx];
      force(sn.out);
    }
  }

  Lit at(uint32_t slot) const { return slot != UNUSED_INPUT ? values_[slot] : kFalse; }

  Lit active(Lit value, Polarity polarity) const {
    return polarity == Polarity::ACTIVE_HIGH ? value : lit_not(value);
  }

  Lit latch(const SimNode& sn) {
    const CompiledFFConfig& cfg = cg_.latch_configs.at(sn.ff_cfg);
    const Lit enable = active(at(sn.in1), cfg.enable_polarity);
    const Lit clear = (cfg.has_clear && sn.in2 != UNUSED_INPUT)
                          ? active(at(sn.in2), cfg.clear_polarity)
                          : kFalse;
    const Lit preset = (cfg.has_preset && sn.in3 != UNUSED_INPUT)
                           ? active(at(sn.in3), cfg.preset_polarity)
                           : kFalse;
    Lit& state = latches_[sn.out];
    const Lit open = sym_.mux(enable, state, at(sn.in0));
    state = sym_.mux(clear, sym_.mux(preset, open, lit_of(cfg.preset_value != 0)),
                     sym_.mux(preset, lit_of(cfg.clear_value != 0),
                              lit_of(cfg.clear_preset_conflict_value != 0)));
    return state;
  }

  Lit icg(const SimNode& sn) {
    const Lit clk = at(sn.in0);
    const Lit enable = sym_.or_(at(sn.in1), at(sn.in2));
    Lit& state = latches_[sn.out];
    if (sn.type == GateType::ICG_POS) {
      state = sym_.mux(clk, enable, state);
      return sym_.and_(clk, state);
    }
    state = sym_.mux(clk, state, enable);
    return sym_.or_(clk, lit_not(state));
  }

  Lit gate(size_t i) {
    const int n = plan_.arity[i];
    std::array<Lit, 6> in{};
    for (int k = 0; k < n; ++k) {
      in[static_cast<size_t>(k)] = values_[plan_.inputs[i][static_cast<size_t>(k)]];
    }
    if (memo_out_[i] >= 0 && memo_in_[i] == in) {
      return memo_out_[i];
    }
    const Lit out = sym_.apply(plan_.table[i], n, in.data());
    memo_in_[i] = in;
    memo_out_[i] = out;
    return out;
  }

  void evaluate() {
    for (int i = plan_.eval_begin; i < plan_.eval_end; ++i) {
      const SimNode& sn = cg_.nodes[static_cast<size_t>(i)];
      Lit v;
      switch (sn.type) {
        case GateType::INPUT:
        case GateType::DFF:
          continue;
        case GateType::CONST0:
          v = kFalse;
          break;
        case GateType::CONST1:
          v = kTrue;
          break;
        case GateType::CONSTX:
          v = sym_.fresh();
          break;
        case GateType::LATCH:
          v = latch(sn);
          break;
        default:
          v = is_icg(sn.type) ? icg(sn) : gate(static_cast<size_t>(i));
      }
      values_[sn.out] = v;
      force(sn.out);
    }
  }

  // GoldenRefSimX's update_ff_states on literals.
  void update_ff_states(std::vector<Lit>& ff_states, bool settled) {
    const std::vector<Lit>& sampled = settled ? prev_ : values_;
    std::vector<Lit> next = ff_states;
    for (size_t idx = 0; idx < cg_.ff_nodes.size(); ++idx) {
      const SimNode& sn = cg_.nodes[static_cast<size_t>(cg_.ff_nodes[idx])];
      const CompiledFFConfig& cfg = cg_.ff_configs.at(sn.ff_cfg);
      const Lit q = ff_states[idx];
      const bool scan_mux =
          cfg.has_scan && sn.in4 != UNUSED_INPUT && sn.in5 != UNUSED_INPUT;
      const Lit scan_active =
          scan_mux ? active(values_[sn.in5], cfg.scan_enable_polarity) : kFalse;
      const Lit not_shifting = lit_not(scan_active);
      const Lit clear =
          (cfg.has_clear && sn.in2 != UNUSED_INPUT)
              ? sym_.and_(not_shifting, active(values_[sn.in2], cfg.clear_polarity))
              : kFalse;
      const Lit preset =
          (cfg.has_preset && sn.in3 != UNUSED_INPUT)
              ? sym_.and_(not_shifting, active(values_[sn.in3], cfg.preset_polarity))
              : kFalse;
      Lit data = sampled[sn.in0];
      if (cfg.has_enable && sn.in2 != UNUSED_INPUT) {
        data = sym_.mux(active(sampled[sn.in2], cfg.enable_polarity), q, data);
      }
      const Lit capture =
          scan_mux ? sym_.mux(active(sampled[sn.in5], cfg.scan_enable_polarity), data,
                              sampled[sn.in4])
                   : data;
      const Lit edge = cfg.trigger == TriggerType::POSEDGE
                           ? sym_.and_(lit_not(prev_[sn.in1]), values_[sn.in1])
                           : sym_.and_(prev_[sn.in1], lit_not(values_[sn.in1]));
      const Lit clocked = sym_.mux(edge, q, capture);
      next[idx] = sym_.mux(clear, sym_.mux(preset, clocked, lit_of(cfg.preset_value != 0)),
                           sym_.mux(preset, lit_of(cfg.clear_value != 0),
                                    lit_of(cfg.clear_preset_conflict_value != 0)));
    }
    ff_states = std::move(next);
  }

  const Plan& plan_;
  const CompiledSimGraph& cg_;
  const CompactFault& fault_;
  Symbolic& sym_;
  std::optional<std::chrono::steady_clock::time_point> deadline_;
  bool active_ = false;
  std::vector<Lit> values_;
  std::vector<Lit> prev_;
  std::vector<Lit> latches_;
  std::vector<std::array<Lit, 6>> memo_in_;
  std::vector<Lit> memo_out_;
};

// 10 SAT, 20 UNSAT, 0 out of time: the matches up to sample `upto` together.
int solve_upto(CaDiCaL::Solver& solver, const std::vector<Match>& matches, int32_t upto) {
  for (const Match& m : matches) {
    if (m.sample <= upto) {
      solver.assume(m.lit);
    }
  }
  return solver.solve();
}

Decision decide(const Plan& plan, const CompactFault& fault, const XMotRequest& request) {
  Decision decision;
  std::optional<std::chrono::steady_clock::time_point> deadline;
  if (request.seconds_per_fault > 0) {
    deadline = std::chrono::steady_clock::now() +
               std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                   std::chrono::duration<double>(request.seconds_per_fault));
  }
  CaDiCaL::Solver solver;
  std::optional<atpg::WallClockTerminator> terminator;
  if (deadline) {
    terminator.emplace(*deadline);
    solver.connect_terminator(&*terminator);
  }
  Symbolic sym(solver, request.max_variables);
  std::vector<Match> matches;
  int32_t conflict = -1;
  std::vector<Lit> power_up;
  try {
    FaultyRun run(plan, fault, sym, deadline);
    run.run(matches, conflict, power_up);
  } catch (const OutOfBudget&) {
    return decision;
  }
  const auto escape = [&](bool model) {
    decision.outcome = MotOutcome::ESCAPES;
    if (request.witness) {
      for (size_t i = 0; i < plan.cg->ff_nodes.size(); ++i) {
        const Lit p = i < power_up.size() ? power_up[i] : kFalse;
        if (is_const(p)) {
          decision.witness.push_back(-1);
        } else {
          const bool seen = model && var_of(p) <= solver.vars();
          decision.witness.push_back(
              static_cast<int8_t>(seen && solver.val(var_of(p)) > 0 ? 1 : 0));
        }
      }
    }
  };
  if (conflict < 0) {
    if (matches.empty()) {
      escape(false);
      return decision;
    }
    const int all = solve_upto(solver, matches, matches.back().sample);
    if (all == 10) {
      escape(true);
      return decision;
    }
    if (all != 20) {
      return decision;
    }
  }
  // Some prefix fails every completion: the shortest. `top` fails; search the
  // samples before it that carry a match.
  int32_t top = conflict >= 0 ? conflict : matches.back().sample;
  std::vector<int32_t> samples;
  for (const Match& m : matches) {
    if (m.sample < top && (samples.empty() || samples.back() != m.sample)) {
      samples.push_back(m.sample);
    }
  }
  size_t lo = 0;
  size_t hi = samples.size();  // samples[hi] (or top) is known to fail
  while (lo < hi) {
    const size_t mid = (lo + hi) / 2;
    const int r = solve_upto(solver, matches, samples[mid]);
    if (r == 0) {
      return decision;
    }
    if (r == 20) {
      hi = mid;
    } else {
      lo = mid + 1;
    }
  }
  decision.outcome = MotOutcome::DETECTED;
  decision.by = hi < samples.size() ? samples[hi] : top;
  return decision;
}

Plan make_plan(const CachedGraph& graph, const XMotRequest& request) {
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;
  Plan plan;
  plan.cg = &cg;
  plan.vec = sequence_vector_x(parsed, cg, request.sequence, plan.options);
  std::vector<int> observe_yids;
  sequence_observed(parsed, cg, request.sequence.base.observe_outputs, observe_yids,
                    plan.observe);
  const GoldenRefSimX ref;
  for (const XSnapshot& sample :
       ref.simulate_sequence(cg, plan.vec, nullptr, plan.options, &observe_yids)) {
    std::vector<Logic> row;
    for (int yid : observe_yids) {
      row.push_back(sample.at(yid));
    }
    plan.golden.push_back(std::move(row));
  }
  if (!request.compare.empty() && request.compare.size() != plan.golden.size()) {
    throw std::runtime_error("sequence grade: compare needs one row per sampled cycle");
  }
  for (size_t s = 0; s < plan.golden.size(); ++s) {
    const std::vector<bool> every(plan.observe.size(), true);
    const std::vector<bool>& row = request.compare.empty() ? every : request.compare[s];
    if (row.size() != plan.observe.size()) {
      throw std::runtime_error("sequence grade: compare row " + std::to_string(s) +
                               " does not give one flag per observed output");
    }
    plan.compare.push_back(row);
  }

  plan.eval_begin = 0;
  plan.eval_end = static_cast<int>(cg.nodes.size());
  if (cg.level_starts.size() >= 2) {
    plan.eval_begin = cg.level_starts.front();
    plan.eval_end = cg.level_starts.back();
  }
  plan.table.assign(cg.nodes.size(), 0);
  plan.arity.assign(cg.nodes.size(), 0);
  plan.inputs.assign(cg.nodes.size(), {});
  std::map<std::pair<GateType, int>, uint64_t> tables;
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const SimNode& sn = cg.nodes[i];
    if (!plain_gate(sn.type)) {
      continue;
    }
    int n = 0;
    for (uint32_t slot : {sn.in0, sn.in1, sn.in2, sn.in3, sn.in4, sn.in5}) {
      if (slot != UNUSED_INPUT) {
        plan.inputs[i][static_cast<size_t>(n++)] = slot;
      }
    }
    plan.arity[i] = static_cast<uint8_t>(n);
    const auto [it, fresh] = tables.try_emplace({sn.type, n}, 0);
    if (fresh) {
      std::vector<bool> bits(static_cast<size_t>(n));
      for (uint32_t a = 0; a < (1u << n); ++a) {
        for (int k = 0; k < n; ++k) {
          bits[static_cast<size_t>(k)] = ((a >> k) & 1u) != 0;
        }
        it->second |= static_cast<uint64_t>(eval_gate_scalar(sn.type, bits)) << a;
      }
    }
    plan.table[i] = it->second;
  }

  std::map<int, size_t> pi_of;
  for (size_t j = 0; j < cg.pi_nets.size(); ++j) {
    pi_of[cg.compiled_to_yosys[static_cast<size_t>(cg.pi_nets[j])]] = j;
  }
  int32_t sample = 0;
  for (size_t c = 0; c < plan.vec.cycles.size(); ++c) {
    const TestCycle& cycle = plan.vec.cycles[c];
    std::vector<uint8_t> value(cg.pi_nets.size(), 0);
    std::vector<uint8_t> unknown(cg.pi_nets.size(), 0);
    for (const auto& [yid, v] : cycle.inputs) {
      const auto it = pi_of.find(yid);
      if (it != pi_of.end()) {
        value[it->second] = v ? 1 : 0;
      }
    }
    if (c < plan.options.unknown_inputs.size()) {
      for (int yid : plan.options.unknown_inputs[c]) {
        const auto it = pi_of.find(yid);
        if (it != pi_of.end()) {
          unknown[it->second] = 1;
        }
      }
    }
    plan.pi_value.push_back(std::move(value));
    plan.pi_unknown.push_back(std::move(unknown));
    if (cycle.sample_outputs) {
      const auto& golden = plan.golden[static_cast<size_t>(sample)];
      for (size_t o = 0; o < plan.observe.size(); ++o) {
        if (plan.compare[static_cast<size_t>(sample)][o] && is_known(golden[o])) {
          plan.last_cycle = static_cast<int>(c);
        }
      }
      ++sample;
    }
  }
  return plan;
}

}  // namespace

XMotResult grade_sequence_faults_mot_x(const std::string& json_path,
                                       const std::string& cell_map_path,
                                       const XMotRequest& request,
                                       const std::string& unsupported_policy,
                                       const std::vector<std::string>& blackbox_instances,
                                       int sim_threads) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const CompiledSimGraph& cg = graph.cg;
  const Plan plan = make_plan(graph, request);
  const auto& specs = request.sequence.base.faults;
  XMotResult result;
  result.golden = plan.golden;
  result.outcome.assign(specs.size(), MotOutcome::UNDECIDED);
  result.detected_by.assign(specs.size(), -1);
  result.witness.assign(specs.size(), {});
  std::vector<CompactFault> faults;
  for (const SequenceFaultSpec& spec : specs) {
    faults.push_back(sequence_fault(spec, cg));
  }
  const bool modelled =
      std::none_of(cg.loop_levels.begin(), cg.loop_levels.end(),
                   [](uint8_t l) { return l != 0; }) &&
      std::none_of(cg.nodes.begin(), cg.nodes.end(),
                   [](const SimNode& sn) { return unsupported_node(sn.type); });
  if (!modelled) {
    return result;
  }
  parallel_ranges(static_cast<int>(faults.size()), effective_sim_threads(sim_threads),
                  [&](int k) {
                    const size_t i = static_cast<size_t>(k);
                    Decision d = decide(plan, faults[i], request);
                    result.outcome[i] = d.outcome;
                    result.detected_by[i] = d.by;
                    result.witness[i] = std::move(d.witness);
                  });
  return result;
}

}  // namespace faultflow
