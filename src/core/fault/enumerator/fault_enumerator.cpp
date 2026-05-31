#include "fault/enumerator/fault_enumerator.hpp"

namespace faultflow {

std::vector<CompactFault> enumerate_faults(const NormalizedGraph& ng,
                                           const CompiledSimGraph& cg,
                                           EnumeratorOptions opt) {
  std::vector<CompactFault> faults;
  for (const auto& [yid, net] : ng.nets) {
    if (net.is_blackboxed) {
      continue;
    }
    if (!cg.yosys_to_compiled.count(yid)) {
      continue;
    }
    const uint32_t cidx = static_cast<uint32_t>(cg.yosys_to_compiled.at(yid));

    for (int t = 0; t < 2; ++t) {
      CompactFault f;
      f.net_index = cidx;
      f.type = (t == 0) ? FaultType::SA0 : FaultType::SA1;
      f.bit = 1;
      f.sa_mask = 1ULL;

      if (net.is_clock && !opt.include_clock_faults) {
        f.exclusion = FaultExclusion::CLOCK;
      } else if (net.is_reset && !opt.include_reset_faults) {
        f.exclusion = FaultExclusion::RESET;
      } else if (net.is_blackboxed) {
        f.exclusion = FaultExclusion::BLACKBOX;
      }
      faults.push_back(f);
    }
  }
  return faults;
}

}  // namespace faultflow
