#include "atpg/cone.hpp"

#include "common/types.hpp"

namespace faultflow::atpg {

std::vector<int> build_driver_index(const CompiledSimGraph& cg) {
  std::vector<int> driver(static_cast<size_t>(cg.net_count), -1);
  for (size_t i = 0; i < cg.nodes.size(); ++i) {
    const uint32_t out = cg.nodes[i].out;
    if (out < static_cast<uint32_t>(cg.net_count)) {
      driver[out] = static_cast<int>(i);
    }
  }
  return driver;
}

FaultCone extract_fault_cone(const CompiledSimGraph& cg, uint32_t fault_net,
                             const std::vector<int>& driver,
                             const std::vector<char>& observable) {
  const size_t n = static_cast<size_t>(cg.net_count);
  FaultCone cone;
  cone.in_outcone.assign(n, 0);
  cone.in_support.assign(n, 0);

  // Output cone O: forward BFS over the fanout CSR (net -> downstream nets).
  // Collect the visited nets so the support seed and observable scan touch only
  // the cone, not the whole circuit.
  std::vector<uint32_t> outcone_nets;
  std::vector<uint32_t> work;
  cone.in_outcone[fault_net] = 1;
  outcone_nets.push_back(fault_net);
  work.push_back(fault_net);
  while (!work.empty()) {
    const uint32_t net = work.back();
    work.pop_back();
    for (uint32_t i = cg.fanout_offsets[net]; i < cg.fanout_offsets[net + 1];
         ++i) {
      const uint32_t t = cg.fanout_targets[i];
      if (!cone.in_outcone[t]) {
        cone.in_outcone[t] = 1;
        outcone_nets.push_back(t);
        work.push_back(t);
      }
    }
  }

  // Reached observables R = O ∩ observable; seed the support set with O.
  std::vector<uint32_t> back;
  back.reserve(outcone_nets.size());
  for (uint32_t net : outcone_nets) {
    cone.in_support[net] = 1;
    back.push_back(net);
    if (net < observable.size() && observable[net]) {
      cone.reached_observables.push_back(net);
    }
  }

  // Support S: backward BFS via driver + node inputs (fan-in of the out-cone).
  while (!back.empty()) {
    const uint32_t net = back.back();
    back.pop_back();
    const int d = driver[net];
    if (d < 0) {
      continue;  // PI / source net: nothing upstream
    }
    const SimNode& node = cg.nodes[static_cast<size_t>(d)];
    const uint32_t ins[] = {node.in0, node.in1, node.in2,
                            node.in3, node.in4, node.in5};
    for (uint32_t in : ins) {
      if (in != UNUSED_INPUT && !cone.in_support[in]) {
        cone.in_support[in] = 1;
        back.push_back(in);
      }
    }
  }
  return cone;
}

FaultStructuralReason structural_reason(const CompiledSimGraph& cg,
                                        uint32_t fault_net,
                                        const std::vector<int>& driver,
                                        const std::vector<char>& observable,
                                        const std::vector<char>& controllable) {
  const size_t n = static_cast<size_t>(cg.net_count);
  FaultStructuralReason r;

  // Forward: does the site reach any observable? Early-out on the first hit.
  std::vector<char> seen_f(n, 0);
  std::vector<uint32_t> work;
  seen_f[fault_net] = 1;
  work.push_back(fault_net);
  if (fault_net < observable.size() && observable[fault_net]) {
    r.reaches_observable = true;
  }
  while (!work.empty() && !r.reaches_observable) {
    const uint32_t net = work.back();
    work.pop_back();
    for (uint32_t i = cg.fanout_offsets[net]; i < cg.fanout_offsets[net + 1];
         ++i) {
      const uint32_t t = cg.fanout_targets[i];
      if (!seen_f[t]) {
        seen_f[t] = 1;
        if (t < observable.size() && observable[t]) {
          r.reaches_observable = true;
          break;
        }
        work.push_back(t);
      }
    }
  }

  // Backward: is the site reachable from any controllable point (PI/pseudo-PI)?
  std::vector<char> seen_b(n, 0);
  std::vector<uint32_t> back;
  seen_b[fault_net] = 1;
  back.push_back(fault_net);
  if (fault_net < controllable.size() && controllable[fault_net]) {
    r.reachable_from_pi = true;
  }
  while (!back.empty() && !r.reachable_from_pi) {
    const uint32_t net = back.back();
    back.pop_back();
    const int d = driver[net];
    if (d < 0) {
      continue;  // source net with no driving node
    }
    const SimNode& node = cg.nodes[static_cast<size_t>(d)];
    const uint32_t ins[] = {node.in0, node.in1, node.in2,
                            node.in3, node.in4, node.in5};
    for (uint32_t in : ins) {
      if (in != UNUSED_INPUT && !seen_b[in]) {
        seen_b[in] = 1;
        if (in < controllable.size() && controllable[in]) {
          r.reachable_from_pi = true;
          break;
        }
        back.push_back(in);
      }
    }
  }
  return r;
}

}  // namespace faultflow::atpg
