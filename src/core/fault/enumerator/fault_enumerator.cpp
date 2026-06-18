#include "fault/enumerator/fault_enumerator.hpp"

namespace faultflow {

std::vector<CompactFault> enumerate_faults(const NormalizedGraph& ng,
                                           const CompiledSimGraph& cg,
                                           EnumeratorOptions opt) {
  std::vector<CompactFault> faults;
  for (uint32_t cidx = 0; cidx < static_cast<uint32_t>(cg.net_count); ++cidx) {
    const int yid = cg.compiled_to_yosys[cidx];
    const NormNet* net = nullptr;
    if (ng.nets.count(yid)) {
      net = &ng.nets.at(yid);
    }

    for (int t = 0; t < 2; ++t) {
      CompactFault f;
      f.net_index = cidx;
      f.type = (t == 0) ? FaultType::SA0 : FaultType::SA1;
      f.bit = 1;
      f.sa_mask = 1ULL;

      if (net != nullptr) {
        if (net->is_clock && !opt.include_clock_faults) {
          f.exclusion = FaultExclusion::CLOCK;
        } else if (net->is_reset && !opt.include_reset_faults) {
          f.exclusion = FaultExclusion::RESET;
        } else if (net->is_blackboxed) {
          f.exclusion = FaultExclusion::BLACKBOX;
        }
      }
      faults.push_back(f);
    }
  }
  return faults;
}

std::vector<CompactFault> enumerate_transition_faults(const NormalizedGraph& ng,
                                                       const CompiledSimGraph& cg,
                                                       EnumeratorOptions opt) {
  auto faults = enumerate_faults(ng, cg, opt);
  for (auto& f : faults) {
    f.model = FaultModel::TRANSITION;
  }
  return faults;
}

}  // namespace faultflow
