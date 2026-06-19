#include "scan/scan_pattern_sim.hpp"

#include <cmath>
#include <stdexcept>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/compiled_graph/graph_cache.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::scan {
namespace {

static bool clock_off(const ScanPatternRequest& req, size_t i) {
  return i < req.clock_off_states.size() ? req.clock_off_states[i] : false;
}

TestCycle make_cycle(const ParsedGraph& parsed,
                     const std::vector<std::string>& clock_ports,
                     const std::vector<bool>& clock_off_states,
                     const std::map<std::string, bool>& values,
                     bool sample_outputs, bool fault_active) {
  TestCycle cycle;
  cycle.sample_outputs = sample_outputs;
  cycle.fault_active = fault_active;
  for (const auto& [name, value] : values) {
    cycle.inputs[parsed.net_id_by_name(name)] = value;
  }
  for (size_t i = 0; i < clock_ports.size(); ++i) {
    if (!values.count(clock_ports[i])) {
      const bool off = i < clock_off_states.size() ? clock_off_states[i] : false;
      cycle.inputs[parsed.net_id_by_name(clock_ports[i])] = off;
    }
  }
  return cycle;
}

void append_clock_pulse(TestVector& vec, const ParsedGraph& parsed,
                        const std::vector<std::string>& clock_ports,
                        const std::vector<bool>& clock_off_states,
                        std::map<std::string, bool> values, bool sample,
                        bool fault_active) {
  for (size_t i = 0; i < clock_ports.size(); ++i)
    values[clock_ports[i]] = i < clock_off_states.size() ? clock_off_states[i] : false;
  vec.cycles.push_back(
      make_cycle(parsed, clock_ports, clock_off_states, values, false, fault_active));
  for (size_t i = 0; i < clock_ports.size(); ++i)
    values[clock_ports[i]] = !(i < clock_off_states.size() ? clock_off_states[i] : false);
  vec.cycles.push_back(
      make_cycle(parsed, clock_ports, clock_off_states, values, sample, fault_active));
}

void append_launch_pulse(TestVector& vec, const ParsedGraph& parsed,
                         const ScanPatternRequest& req,
                         std::map<std::string, bool> values) {
  // LOC launch: one functional clock with the fault INACTIVE establishes the
  // launched (frame-0) state. Sample at the inactive clock level so the
  // transition qualifier can read the good-machine pre-transition value at the
  // fault net; functional POs are read at the capture pulse, not here.
  // Only active-domain clocks are pulsed; others hold at their off-state.
  for (size_t i = 0; i < req.clock_ports.size(); ++i)
    values[req.clock_ports[i]] = clock_off(req, i);
  vec.cycles.push_back(
      make_cycle(parsed, req.clock_ports, req.clock_off_states, values, true, false));
  for (size_t i = 0; i < req.clock_ports.size(); ++i)
    values[req.clock_ports[i]] = is_active(req, i) ? !clock_off(req, i) : clock_off(req, i);
  vec.cycles.push_back(
      make_cycle(parsed, req.clock_ports, req.clock_off_states, values, false, false));
}

void append_launch_shift(TestVector& vec, const ParsedGraph& parsed,
                         const ScanPatternRequest& request,
                         std::map<std::string, bool> values) {
  // LOS launch: ONE extra scan shift with scan_enable ASSERTED, feeding the
  // fresh launch scan-in bit at each chain head. Fault INACTIVE; sample frame 0
  // at the inactive clock level so the transition qualifier can read the good
  // pre-transition value. scan_enable is local to this shift (values is taken by
  // value), so the capture pulse built from the SE=0 base map still de-asserts.
  // Only active-domain clocks are pulsed during the LOS launch shift.
  values[request.scan_enable_port] = true;
  for (size_t chain_id = 0; chain_id < request.scan_input_ports.size();
       ++chain_id) {
    const auto it = request.los_launch_scan_in.find(static_cast<int>(chain_id));
    values[request.scan_input_ports[chain_id]] =
        it != request.los_launch_scan_in.end() && it->second;
  }
  for (size_t i = 0; i < request.clock_ports.size(); ++i)
    values[request.clock_ports[i]] = clock_off(request, i);
  vec.cycles.push_back(make_cycle(parsed, request.clock_ports,
                                  request.clock_off_states, values, true, false));
  for (size_t i = 0; i < request.clock_ports.size(); ++i)
    values[request.clock_ports[i]] =
        is_active(request, i) ? !clock_off(request, i) : clock_off(request, i);
  vec.cycles.push_back(make_cycle(parsed, request.clock_ports,
                                  request.clock_off_states, values, false, false));
}

void append_capture_pulse(TestVector& vec, const ParsedGraph& parsed,
                          const ScanPatternRequest& req,
                          std::map<std::string, bool> values) {
  // Functional POs belong to the loaded-state combinational response. Sample
  // after settling at the inactive clock level, then capture PPO values.
  // Only active-domain clocks are pulsed for the capture edge.
  for (size_t i = 0; i < req.clock_ports.size(); ++i)
    values[req.clock_ports[i]] = clock_off(req, i);
  vec.cycles.push_back(
      make_cycle(parsed, req.clock_ports, req.clock_off_states, values, true, true));
  for (size_t i = 0; i < req.clock_ports.size(); ++i)
    values[req.clock_ports[i]] = is_active(req, i) ? !clock_off(req, i) : clock_off(req, i);
  vec.cycles.push_back(
      make_cycle(parsed, req.clock_ports, req.clock_off_states, values, false, true));
}

void append_unload_pulse(TestVector& vec, const ParsedGraph& parsed,
                         const std::vector<std::string>& clock_ports,
                         const std::vector<bool>& clock_off_states,
                         std::map<std::string, bool> values) {
  for (size_t i = 0; i < clock_ports.size(); ++i)
    values[clock_ports[i]] = i < clock_off_states.size() ? clock_off_states[i] : false;
  vec.cycles.push_back(make_cycle(parsed, clock_ports, clock_off_states, values, true, true));
  for (size_t i = 0; i < clock_ports.size(); ++i)
    values[clock_ports[i]] = !(i < clock_off_states.size() ? clock_off_states[i] : false);
  vec.cycles.push_back(make_cycle(parsed, clock_ports, clock_off_states, values, false, true));
}

std::map<std::string, bool> base_values(const ScanPatternRequest& request) {
  std::map<std::string, bool> values = request.capture_pi_values;
  values[request.scan_enable_port] = false;
  for (size_t i = 0; i < request.clock_ports.size(); ++i)
    values[request.clock_ports[i]] = clock_off(request, i);
  for (const auto& scan_in : request.scan_input_ports) {
    values[scan_in] = false;
  }
  return values;
}

bool sample_bit(const std::map<int, bool>& sample, const ParsedGraph& parsed,
                const std::string& port) {
  const int yid = parsed.net_id_by_name(port);
  const auto it = sample.find(yid);
  if (it == sample.end()) {
    throw std::runtime_error("scan pattern sample missing port: " + port);
  }
  return it->second;
}

TestVector build_scan_pattern_vector(const ParsedGraph& parsed,
                                     const ScanPatternRequest& request) {
  if (request.max_chain_length < 0) {
    throw std::runtime_error("max_chain_length must be >= 0");
  }
  if (request.scan_input_ports.size() != request.scan_output_ports.size()) {
    throw std::runtime_error("scan input/output port count mismatch");
  }

  TestVector vec;
  for (int offset = 0; offset < request.max_chain_length; ++offset) {
    std::map<std::string, bool> values = base_values(request);
    values[request.scan_enable_port] = true;
    for (size_t chain_id = 0; chain_id < request.scan_input_ports.size();
         ++chain_id) {
      const auto it = request.load_seqs.find(static_cast<int>(chain_id));
      const std::vector<bool>& bits =
          it == request.load_seqs.end() ? std::vector<bool>{} : it->second;
      const bool bit =
          offset < static_cast<int>(bits.size()) ? bits[offset] : false;
      values[request.scan_input_ports[chain_id]] = bit;
    }
    append_clock_pulse(vec, parsed, request.clock_ports, request.clock_off_states,
                       values, false, false);
  }

  {
    std::map<std::string, bool> values = request.capture_pi_values;
    values[request.scan_enable_port] = false;
    for (size_t i = 0; i < request.clock_ports.size(); ++i)
      values[request.clock_ports[i]] = clock_off(request, i);
    for (const auto& scan_in : request.scan_input_ports) {
      values[scan_in] = false;
    }
    if (request.loc_two_capture && request.los_two_capture) {
      throw std::runtime_error(
          "loc_two_capture and los_two_capture are mutually exclusive");
    }
    if (request.loc_two_capture) {
      append_launch_pulse(vec, parsed, request, values);
    } else if (request.los_two_capture) {
      append_launch_shift(vec, parsed, request, values);
    }
    append_capture_pulse(vec, parsed, request, values);
  }

  for (int offset = 0; offset < request.max_chain_length; ++offset) {
    std::map<std::string, bool> values = base_values(request);
    values[request.scan_enable_port] = true;
    append_unload_pulse(vec, parsed, request.clock_ports,
                        request.clock_off_states, values);
  }
  return vec;
}

ScanPatternResult extract_scan_observations(
    const ParsedGraph& parsed, const ScanPatternRequest& request,
    const std::vector<std::map<int, bool>>& samples) {
  if (samples.empty()) {
    throw std::runtime_error("scan pattern produced no samples");
  }
  // LOC inserts a leading launch sample (frame 0); functional POs and the
  // unload then shift one index later. Non-LOC: functional_idx=0, unload at 1.
  const int functional_idx =
      (request.loc_two_capture || request.los_two_capture) ? 1 : 0;
  const int unload_start = functional_idx + 1;
  if (static_cast<int>(samples.size()) !=
      request.max_chain_length + unload_start) {
    throw std::runtime_error("scan pattern sample count mismatch");
  }

  ScanPatternResult result;
  const std::map<int, bool>& capture_sample =
      samples[static_cast<size_t>(functional_idx)];
  for (const auto& port : request.functional_output_ports) {
    result.real_po_values[port] = sample_bit(capture_sample, parsed, port);
  }

  for (size_t chain_id = 0; chain_id < request.scan_output_ports.size();
       ++chain_id) {
    const std::string& scan_out = request.scan_output_ports[chain_id];
    std::vector<bool> bits;
    bits.reserve(request.max_chain_length);
    for (int offset = 0; offset < request.max_chain_length; ++offset) {
      bits.push_back(sample_bit(samples[static_cast<size_t>(unload_start + offset)],
                                parsed, scan_out));
    }
    result.unload_seqs[static_cast<int>(chain_id)] = std::move(bits);
  }
  return result;
}

CompactFault make_compact_fault(const ScanProtocolFaultSpec& spec) {
  CompactFault fault;
  fault.net_index = spec.compiled_net_index;
  fault.type = spec.fault_type == 0 ? FaultType::SA0 : FaultType::SA1;
  fault.bit = 1;
  fault.sa_mask = 1ULL;
  fault.exclusion = FaultExclusion::NONE;
  return fault;
}

uint64_t sample_word(const std::vector<uint64_t>& sample,
                     const ParsedGraph& parsed, const CompiledSimGraph& cg,
                     const std::string& port) {
  const int yid = parsed.net_id_by_name(port);
  const auto it = cg.yosys_to_compiled.find(yid);
  if (it == cg.yosys_to_compiled.end()) {
    throw std::runtime_error("scan pattern sample missing port: " + port);
  }
  return sample.at(static_cast<size_t>(it->second));
}

bool lane_bit(uint64_t word, int bit) {
  return ((word >> bit) & 1ULL) != 0;
}

ScanPatternResult extract_scan_lane_observations(
    const ParsedGraph& parsed, const CompiledSimGraph& cg,
    const ScanPatternRequest& request,
    const std::vector<std::vector<uint64_t>>& samples, int bit) {
  const int functional_idx =
      (request.loc_two_capture || request.los_two_capture) ? 1 : 0;
  const int unload_start = functional_idx + 1;
  if (static_cast<int>(samples.size()) !=
      request.max_chain_length + unload_start) {
    throw std::runtime_error("scan pattern sample count mismatch");
  }
  ScanPatternResult result;
  for (const auto& port : request.functional_output_ports) {
    result.real_po_values[port] = lane_bit(
        sample_word(samples[static_cast<size_t>(functional_idx)], parsed, cg,
                    port),
        bit);
  }
  for (size_t chain_id = 0; chain_id < request.scan_output_ports.size();
       ++chain_id) {
    std::vector<bool> bits;
    bits.reserve(request.max_chain_length);
    for (int offset = 0; offset < request.max_chain_length; ++offset) {
      bits.push_back(lane_bit(
          sample_word(samples.at(static_cast<size_t>(unload_start + offset)),
                      parsed, cg, request.scan_output_ports.at(chain_id)),
          bit));
    }
    result.unload_seqs[static_cast<int>(chain_id)] = std::move(bits);
  }
  return result;
}

}  // namespace

bool scan_observations_equal(const ScanPatternResult& lhs,
                             const ScanPatternResult& rhs) {
  return lhs.real_po_values == rhs.real_po_values &&
         lhs.unload_seqs == rhs.unload_seqs;
}

ScanPatternResult simulate_scan_pattern(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanPatternRequest& request, const std::string& unsupported_policy) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;

  const TestVector vec = build_scan_pattern_vector(parsed, request);
  GoldenRefSim sim;
  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  return extract_scan_observations(parsed, request, samples);
}

ScanProtocolFaultSimResult simulate_scan_protocol_faults(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanProtocolFaultRequest& request,
    const std::string& unsupported_policy) {
  const CachedGraph& graph =
      load_cached_graph(json_path, cell_map_path, unsupported_policy);
  const ParsedGraph& parsed = graph.parsed;
  const CompiledSimGraph& cg = graph.cg;

  const TestVector vec = build_scan_pattern_vector(parsed, request.pattern);
  GoldenRefSim golden_sim;
  const auto golden_samples = golden_sim.simulate_sequence_fault_free(cg, vec);

  ScanProtocolFaultSimResult result;
  result.golden =
      extract_scan_observations(parsed, request.pattern, golden_samples);

  if (request.faults.empty()) {
    return result;
  }

  const int batch_count = static_cast<int>(std::ceil(
      static_cast<double>(request.faults.size()) /
      static_cast<double>(kScanProtocolFaultBatchSize)));
  result.batches.reserve(static_cast<size_t>(batch_count));

  for (int batch_idx = 0; batch_idx < batch_count; ++batch_idx) {
    ScanProtocolFaultBatchResult batch;
    batch.batch_index = batch_idx;
    const size_t begin =
        static_cast<size_t>(batch_idx * kScanProtocolFaultBatchSize);
    const size_t end = std::min(begin + static_cast<size_t>(kScanProtocolFaultBatchSize),
                                request.faults.size());
    batch.lanes.reserve(end - begin);

    FaultBatch fault_batch;
    fault_batch.size = static_cast<int>(end - begin);
    for (size_t fault_idx = begin; fault_idx < end; ++fault_idx) {
      const ScanProtocolFaultSpec& spec = request.faults[fault_idx];
      if (spec.compiled_net_index >=
          static_cast<uint32_t>(cg.net_count)) {
        throw std::runtime_error(
            "scan protocol fault compiled_net_index out of range");
      }

      CompactFault fault = make_compact_fault(spec);
      const int lane_bit_index = static_cast<int>(fault_idx - begin) + 1;
      fault.bit = static_cast<uint8_t>(lane_bit_index);
      fault.sa_mask = 1ULL << lane_bit_index;
      fault_batch.faults[fault_idx - begin] = fault;
      fault_batch.mask |= fault.sa_mask;
    }

    BitParallelSim parallel;
    const auto batch_samples =
        parallel.simulate_batch_samples(cg, vec, fault_batch);
    for (size_t fault_idx = begin; fault_idx < end; ++fault_idx) {
      ScanProtocolFaultLaneResult lane;
      lane.fault_index = fault_idx;
      const int lane_bit_index = static_cast<int>(fault_idx - begin) + 1;
      const ScanPatternResult faulty_obs = extract_scan_lane_observations(
          parsed, cg, request.pattern, batch_samples, lane_bit_index);
      bool detected = !scan_observations_equal(result.golden, faulty_obs);

      // LOC transition qualifier: a stuck-at observation difference only counts
      // as a transition fault if the GOOD machine actually made the required
      // edge at the fault net between the launch (frame 0) and capture (frame 1)
      // samples. samples[0]=launch, samples[1]=capture; bit 0 is the good lane.
      if (detected && (request.pattern.loc_two_capture ||
                       request.pattern.los_two_capture)) {
        const ScanProtocolFaultSpec& spec = request.faults[fault_idx];
        const auto net = static_cast<size_t>(spec.compiled_net_index);
        const bool launch_good = (batch_samples[0].at(net) & 1ULL) != 0;
        const bool capture_good = (batch_samples[1].at(net) & 1ULL) != 0;
        const bool pre = spec.fault_type == 1;   // STF pre=1, STR pre=0
        const bool post = spec.fault_type == 0;  // STR post=1, STF post=0
        if (launch_good != pre || capture_good != post) {
          detected = false;
        }
      }

      lane.outcome = detected ? ScanProtocolFaultOutcome::PASS
                              : ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT;
      batch.lanes.push_back(lane);
    }
    result.batches.push_back(std::move(batch));
  }

  return result;
}

}  // namespace faultflow::scan
