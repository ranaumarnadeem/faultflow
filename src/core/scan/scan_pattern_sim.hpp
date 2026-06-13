#pragma once

#include <map>
#include <string>
#include <vector>

namespace faultflow::scan {

struct ScanPatternRequest {
  std::string clock_port;
  std::string scan_enable_port;
  std::vector<std::string> scan_input_ports;
  std::vector<std::string> scan_output_ports;
  std::vector<std::string> functional_output_ports;
  int max_chain_length = 0;
  std::map<int, std::vector<bool>> load_seqs;
  std::map<std::string, bool> capture_pi_values;
};

struct ScanPatternResult {
  std::map<std::string, bool> real_po_values;
  std::map<int, std::vector<bool>> unload_seqs;
};

ScanPatternResult simulate_scan_pattern(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanPatternRequest& request, const std::string& unsupported_policy);

}  // namespace faultflow::scan
