#include "sim/xsim/dual.hpp"

#include <stdexcept>
#include <vector>

#include "sim/gate_eval.hpp"

namespace faultflow {

namespace {

constexpr int kInputs = 6;
constexpr int kMinterms = 1 << kInputs;

// A product term: the inputs in `care`, each at its bit in `value`.
struct Cube {
  uint8_t care = 0;
  uint8_t value = 0;
};

struct GateCubes {
  bool valid = false;
  std::vector<Cube> ones;   // prime implicants of the function
  std::vector<Cube> zeros;  // prime implicants of its complement
  // The same cubes as rails, and the hot loop's view of them.
  std::vector<DualRailCube> rail_ones;
  std::vector<DualRailCube> rail_zeros;
  DualGate gate;
};

// The minterms each cube covers, as a 64-bit set (bit m: minterm m), at
// care * kMinterms + value.
const std::vector<uint64_t>& cube_minterms() {
  static const std::vector<uint64_t> masks = [] {
    std::vector<uint64_t> out(kMinterms * kMinterms, 0ULL);
    for (int care = 0; care < kMinterms; ++care) {
      for (int value = 0; value < kMinterms; ++value) {
        if ((value & ~care) != 0) {
          continue;
        }
        uint64_t mask = 0ULL;
        for (int m = 0; m < kMinterms; ++m) {
          if ((m & care) == value) {
            mask |= 1ULL << m;
          }
        }
        out[static_cast<size_t>(care * kMinterms + value)] = mask;
      }
    }
    return out;
  }();
  return masks;
}

// Every prime implicant of the minterms in `on` (bit m set: the function is the
// target value at m).
std::vector<Cube> primes(uint64_t on) {
  const std::vector<uint64_t>& minterms = cube_minterms();
  const auto implicant = [&](int care, int value) {
    return (minterms[static_cast<size_t>(care * kMinterms + value)] & ~on) == 0ULL;
  };
  std::vector<Cube> out;
  for (int care = 0; care < kMinterms; ++care) {
    for (int value = care;; value = (value - 1) & care) {
      bool prime = implicant(care, value);
      for (int i = 0; prime && i < kInputs; ++i) {
        if ((care >> i) & 1) {
          prime = !implicant(care & ~(1 << i), value & ~(1 << i));
        }
      }
      if (prime) {
        out.push_back({static_cast<uint8_t>(care), static_cast<uint8_t>(value)});
      }
      if (value == 0) {
        break;
      }
    }
  }
  return out;
}

std::vector<DualRailCube> rails_of(const std::vector<Cube>& cubes) {
  std::vector<DualRailCube> out;
  for (const Cube& c : cubes) {
    DualRailCube r;
    for (int i = 0; i < kInputs; ++i) {
      if ((c.care >> i) & 1) {
        r.rail[r.size++] = static_cast<uint8_t>(2 * i + (((c.value >> i) & 1) ? 0 : 1));
      }
    }
    out.push_back(r);
  }
  return out;
}

size_t literals(const std::vector<DualRailCube>& cubes) {
  size_t n = 0;
  for (const DualRailCube& c : cubes) {
    n += c.size + 1;  // its literals, and the OR
  }
  return n;
}

GateCubes cubes_for(GateType type) {
  GateCubes out;
  uint64_t on = 0ULL;
  try {
    for (int m = 0; m < kMinterms; ++m) {
      std::vector<bool> bits(kInputs);
      for (int i = 0; i < kInputs; ++i) {
        bits[i] = ((m >> i) & 1) != 0;
      }
      if (eval_gate_scalar(type, bits)) {
        on |= 1ULL << m;
      }
    }
  } catch (const std::exception&) {
    return out;  // no combinational function (INPUT, DFF)
  }
  out.valid = true;
  out.ones = primes(on);
  out.zeros = primes(~on);
  out.rail_ones = rails_of(out.ones);
  out.rail_zeros = rails_of(out.zeros);
  for (const auto* side : {&out.ones, &out.zeros}) {
    for (const Cube& c : *side) {
      out.gate.relevant |= c.care;
    }
  }
  for (int i = 0; i < kInputs; ++i) {
    if ((out.gate.relevant >> i) & 1) {
      out.gate.arity = static_cast<uint8_t>(i + 1);
    }
  }
  out.gate.known_by_ones = literals(out.rail_ones) <= literals(out.rail_zeros);
  return out;
}

const std::vector<GateCubes>& cube_table() {
  static const std::vector<GateCubes> table = [] {
    std::vector<GateCubes> t;
    for (int g = 0; g <= static_cast<int>(GateType::ICG_EN_CHAIN); ++g) {
      t.push_back(cubes_for(static_cast<GateType>(g)));
    }
    // Point each gate at its rails now that the vectors are in place.
    for (GateCubes& g : t) {
      g.gate.ones = g.rail_ones.data();
      g.gate.ones_end = g.rail_ones.data() + g.rail_ones.size();
      g.gate.zeros = g.rail_zeros.data();
      g.gate.zeros_end = g.rail_zeros.data() + g.rail_zeros.size();
    }
    return t;
  }();
  return table;
}

const DualGate& gate_or_throw(GateType type) {
  const DualGate* g = dual_gate(type);
  if (g == nullptr) {
    throw std::runtime_error("eval_gate_dual: no combinational function for this gate");
  }
  return *g;
}

}  // namespace

const DualGate* dual_gate(GateType type) {
  const auto& table = cube_table();
  const size_t g = static_cast<size_t>(type);
  return g < table.size() && table[g].valid ? &table[g].gate : nullptr;
}

Dual eval_gate_dual(GateType type, const std::array<Dual, 6>& inputs) {
  return eval_dual_gate(gate_or_throw(type), inputs);
}

Dual eval_gate_dual_cubes(GateType type, const std::array<Dual, 6>& inputs) {
  const DualGate& g = gate_or_throw(type);
  uint64_t rails[2 * kInputs];
  for (int i = 0; i < kInputs; ++i) {
    rails[2 * i] = inputs[i].h;
    rails[2 * i + 1] = inputs[i].l;
  }
  return {dual_terms(g.ones, g.ones_end, rails), dual_terms(g.zeros, g.zeros_end, rails)};
}

const DualCubes& dual_cubes(GateType type) {
  static const std::vector<DualCubes> views = [] {
    std::vector<DualCubes> out;
    for (const GateCubes& g : cube_table()) {
      DualCubes v;
      v.valid = g.valid;
      for (const Cube& c : g.ones) {
        v.ones.push_back({c.care, c.value});
      }
      for (const Cube& c : g.zeros) {
        v.zeros.push_back({c.care, c.value});
      }
      out.push_back(std::move(v));
    }
    return out;
  }();
  static const DualCubes kNone;
  const size_t g = static_cast<size_t>(type);
  return g < views.size() ? views[g] : kNone;
}

}  // namespace faultflow
