#pragma once

#include <string>

namespace faultflow {

// How a netlist's graph is built, from the policy string every graph-loading
// entry point takes: the unsupported-cell policy ("fail" or "blackbox"),
// followed by "+x" for an X-mode graph (the X engine's: x/z bits unknown;
// latches, loops and tristate buses once supported). A plain policy builds
// exactly the two-valued graph it always did.
struct GraphPolicy {
  std::string unsupported = "fail";
  bool x_mode = false;
};

inline GraphPolicy parse_graph_policy(const std::string& policy) {
  static const std::string kX = "+x";
  GraphPolicy out;
  if (policy.size() >= kX.size() &&
      policy.compare(policy.size() - kX.size(), kX.size(), kX) == 0) {
    out.unsupported = policy.substr(0, policy.size() - kX.size());
    out.x_mode = true;
  } else {
    out.unsupported = policy;
  }
  return out;
}

}  // namespace faultflow
