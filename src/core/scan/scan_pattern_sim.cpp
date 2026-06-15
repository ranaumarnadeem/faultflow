#include "scan/scan_pattern_sim.hpp"

#include <cmath>
#include <stdexcept>

#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/engine/bit_parallel_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::scan {
namespace {

TestCycle make_cycle(const ParsedGraph& parsed, const std::string& clock_port,
                     const std::map<std::string, bool>& values,
                     bool sample_outputs, bool fault_active) {
  TestCycle cycle;
  cycle.sample_outputs = sample_outputs;
  cycle.fault_active = fault_active;
  for (const auto& [name, value] : values) {
    cycle.inputs[parsed.net_id_by_name(name)] = value;
  }
  if (!values.count(clock_port)) {
    cycle.inputs[parsed.net_id_by_name(clock_port)] = false;
  }
  return cycle;
}

void append_clock_pulse(TestVector& vec, const ParsedGraph& parsed,
                        const std::string& clock_port,
                        std::map<std::string, bool> values, bool sample,
                        bool fault_active) {
  values[clock_port] = false;
  vec.cycles.push_back(
      make_cycle(parsed, clock_port, values, false, fault_active));
  values[clock_port] = true;
  vec.cycles.push_back(
      make_cycle(parsed, clock_port, values, sample, fault_active));
}

void append_capture_pulse(TestVector& vec, const ParsedGraph& parsed,
                          const std::string& clock_port,
                          std::map<std::string, bool> values) {
  // Functional POs belong to the loaded-state combinational response. Sample
  // after settling at the inactive clock level, then capture PPO values.
  values[clock_port] = false;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, true, true));
  values[clock_port] = true;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, false, true));
}

void append_unload_pulse(TestVector& vec, const ParsedGraph& parsed,
                         const std::string& clock_port,
                         std::map<std::string, bool> values) {
  values[clock_port] = false;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, true, true));
  values[clock_port] = true;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, false, true));
}

std::map<std::string, bool> base_values(const ScanPatternRequest& request) {
  std::map<std::string, bool> values = request.capture_pi_values;
  values[request.scan_enable_port] = false;
  values[request.clock_port] = false;
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
    append_clock_pulse(vec, parsed, request.clock_port, values, false, false);
  }

  {
    std::map<std::string, bool> values = request.capture_pi_values;
    values[request.scan_enable_port] = false;
    values[request.clock_port] = false;
    for (const auto& scan_in : request.scan_input_ports) {
      values[scan_in] = false;
    }
    append_capture_pulse(vec, parsed, request.clock_port, values);
  }

  for (int offset = 0; offset < request.max_chain_length; ++offset) {
    std::map<std::string, bool> values = base_values(request);
    values[request.scan_enable_port] = true;
    append_unload_pulse(vec, parsed, request.clock_port, values);
  }
  return vec;
}

ScanPatternResult extract_scan_observations(
    const ParsedGraph& parsed, const ScanPatternRequest& request,
    const std::vector<std::map<int, bool>>& samples) {
  if (samples.empty()) {
    throw std::runtime_error("scan pattern produced no samples");
  }
  if (static_cast<int>(samples.size()) != request.max_chain_length + 1) {
    throw std::runtime_error("scan pattern sample count mismatch");
  }

  ScanPatternResult result;
  const std::map<int, bool>& capture_sample = samples.front();
  for (const auto& port : request.functional_output_ports) {
    result.real_po_values[port] = sample_bit(capture_sample, parsed, port);
  }

  for (size_t chain_id = 0; chain_id < request.scan_output_ports.size();
       ++chain_id) {
    const std::string& scan_out = request.scan_output_ports[chain_id];
    std::vector<bool> bits;
    bits.reserve(request.max_chain_length);
    for (int offset = 1; offset <= request.max_chain_length; ++offset) {
      bits.push_back(sample_bit(samples[offset], parsed, scan_out));
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
  if (static_cast<int>(samples.size()) != request.max_chain_length + 1) {
    throw std::runtime_error("scan pattern sample count mismatch");
  }
  ScanPatternResult result;
  for (const auto& port : request.functional_output_ports) {
    result.real_po_values[port] =
        lane_bit(sample_word(samples.front(), parsed, cg, port), bit);
  }
  for (size_t chain_id = 0; chain_id < request.scan_output_ports.size();
       ++chain_id) {
    std::vector<bool> bits;
    bits.reserve(request.max_chain_length);
    for (int offset = 1; offset <= request.max_chain_length; ++offset) {
      bits.push_back(lane_bit(
          sample_word(samples.at(offset), parsed, cg,
                      request.scan_output_ports.at(chain_id)),
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
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  const TestVector vec = build_scan_pattern_vector(parsed, request);
  GoldenRefSim sim;
  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  return extract_scan_observations(parsed, request, samples);
}

ScanProtocolFaultSimResult simulate_scan_protocol_faults(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanProtocolFaultRequest& request,
    const std::string& unsupported_policy) {
  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

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
      lane.outcome = scan_observations_equal(result.golden, faulty_obs)
                         ? ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT
                         : ScanProtocolFaultOutcome::PASS;
      batch.lanes.push_back(lane);
    }
    result.batches.push_back(std::move(batch));
  }

  return result;
}

}  // namespace faultflow::scan
