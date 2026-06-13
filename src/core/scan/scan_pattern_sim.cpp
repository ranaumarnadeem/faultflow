#include "scan/scan_pattern_sim.hpp"

#include <stdexcept>

#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

namespace faultflow::scan {
namespace {

int net_id_or_throw(const ParsedGraph& parsed, const std::string& name) {
  return parsed.net_id_by_name(name);
}

TestCycle make_cycle(const ParsedGraph& parsed, const std::string& clock_port,
                     const std::map<std::string, bool>& values,
                     bool sample_outputs) {
  TestCycle cycle;
  cycle.sample_outputs = sample_outputs;
  for (const auto& [name, value] : values) {
    cycle.inputs[net_id_or_throw(parsed, name)] = value;
  }
  if (!values.count(clock_port)) {
    cycle.inputs[net_id_or_throw(parsed, clock_port)] = false;
  }
  return cycle;
}

void append_clock_pulse(TestVector& vec, const ParsedGraph& parsed,
                        const std::string& clock_port,
                        std::map<std::string, bool> values, bool sample) {
  values[clock_port] = false;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, false));
  values[clock_port] = true;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, sample));
}

void append_unload_pulse(TestVector& vec, const ParsedGraph& parsed,
                         const std::string& clock_port,
                         std::map<std::string, bool> values) {
  values[clock_port] = false;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, true));
  values[clock_port] = true;
  vec.cycles.push_back(make_cycle(parsed, clock_port, values, false));
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

}  // namespace

ScanPatternResult simulate_scan_pattern(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanPatternRequest& request, const std::string& unsupported_policy) {
  if (request.max_chain_length < 0) {
    throw std::runtime_error("max_chain_length must be >= 0");
  }
  if (request.scan_input_ports.size() != request.scan_output_ports.size()) {
    throw std::runtime_error("scan input/output port count mismatch");
  }

  const ParsedGraph parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const NormalizedGraph ng =
      NormalizedGraph::from_parsed(parsed, cell_map, unsupported_policy);
  const CompiledSimGraph cg = GraphCompiler::compile(ng);

  TestVector vec;
  for (int offset = 0; offset < request.max_chain_length; ++offset) {
    std::map<std::string, bool> values = base_values(request);
    values[request.scan_enable_port] = true;
    for (size_t chain_id = 0; chain_id < request.scan_input_ports.size();
         ++chain_id) {
      const auto it = request.load_seqs.find(static_cast<int>(chain_id));
      const std::vector<bool>& bits =
          it == request.load_seqs.end() ? std::vector<bool>{} : it->second;
      const bool bit = offset < static_cast<int>(bits.size()) ? bits[offset] : false;
      values[request.scan_input_ports[chain_id]] = bit;
    }
    append_clock_pulse(vec, parsed, request.clock_port, values, false);
  }

  {
    std::map<std::string, bool> values = request.capture_pi_values;
    values[request.scan_enable_port] = false;
    values[request.clock_port] = false;
    for (const auto& scan_in : request.scan_input_ports) {
      values[scan_in] = false;
    }
    append_clock_pulse(vec, parsed, request.clock_port, values, true);
  }

  for (int offset = 0; offset < request.max_chain_length; ++offset) {
    std::map<std::string, bool> values = base_values(request);
    values[request.scan_enable_port] = true;
    append_unload_pulse(vec, parsed, request.clock_port, values);
  }

  GoldenRefSim sim;
  const auto samples = sim.simulate_sequence_fault_free(cg, vec);
  if (samples.empty()) {
    throw std::runtime_error("scan pattern produced no samples");
  }
  if (static_cast<int>(samples.size()) != request.max_chain_length + 1) {
    throw std::runtime_error("scan pattern sample count mismatch");
  }

  ScanPatternResult result;
  const std::map<int, bool>& capture_sample = samples.front();
  for (const auto& port : request.functional_output_ports) {
    result.real_po_values[port] =
        sample_bit(capture_sample, parsed, port);
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

}  // namespace faultflow::scan
