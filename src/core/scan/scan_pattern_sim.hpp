#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace faultflow::scan {

constexpr int kScanProtocolFaultBatchSize = 63;

struct ScanPatternRequest {
  std::string clock_port;
  std::string scan_enable_port;
  std::vector<std::string> scan_input_ports;
  std::vector<std::string> scan_output_ports;
  std::vector<std::string> functional_output_ports;
  int max_chain_length = 0;
  std::map<int, std::vector<bool>> load_seqs;
  std::map<std::string, bool> capture_pi_values;
  // Launch-on-capture transition protocol (Phase 4 Step 2b). When true, a
  // fault-free LAUNCH clock is inserted before the (fault-active) capture clock
  // so the response is a two-frame transition rather than a single stuck-at
  // capture. Functional POs are sampled at the capture (frame 1) pulse.
  bool loc_two_capture = false;
};

struct ScanPatternResult {
  std::map<std::string, bool> real_po_values;
  std::map<int, std::vector<bool>> unload_seqs;
};

enum class ScanProtocolFaultOutcome : uint8_t {
  PASS = 0,
  NO_CAPTURE_OR_UNLOAD_EFFECT = 1,
};

struct ScanProtocolFaultSpec {
  uint32_t compiled_net_index = 0;
  uint8_t fault_type = 0;  // 0=SA0, 1=SA1
};

struct ScanProtocolFaultRequest {
  ScanPatternRequest pattern;
  std::vector<ScanProtocolFaultSpec> faults;
};

struct ScanProtocolFaultLaneResult {
  size_t fault_index = 0;
  ScanProtocolFaultOutcome outcome =
      ScanProtocolFaultOutcome::NO_CAPTURE_OR_UNLOAD_EFFECT;
};

struct ScanProtocolFaultBatchResult {
  int batch_index = 0;
  std::vector<ScanProtocolFaultLaneResult> lanes;
};

struct ScanProtocolFaultSimResult {
  ScanPatternResult golden;
  std::vector<ScanProtocolFaultBatchResult> batches;
};

ScanPatternResult simulate_scan_pattern(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanPatternRequest& request, const std::string& unsupported_policy);

ScanProtocolFaultSimResult simulate_scan_protocol_faults(
    const std::string& json_path, const std::string& cell_map_path,
    const ScanProtocolFaultRequest& request,
    const std::string& unsupported_policy);

bool scan_observations_equal(const ScanPatternResult& lhs,
                             const ScanPatternResult& rhs);

}  // namespace faultflow::scan
