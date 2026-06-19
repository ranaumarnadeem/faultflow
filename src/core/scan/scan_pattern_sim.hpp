#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace faultflow::scan {

constexpr int kScanProtocolFaultBatchSize = 63;

struct ScanPatternRequest {
  std::vector<std::string> clock_ports;
  std::vector<bool> clock_off_states;  // parallel to clock_ports; element i is the
                                       // inactive level for clock i (false = default)
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
  // Launch-on-shift transition protocol. When true, the fault-free LAUNCH is the
  // last scan shift (scan_enable asserted, one extra shift feeding the per-chain
  // launch scan-in bit) rather than a functional clock. Mutually exclusive with
  // loc_two_capture.
  bool los_two_capture = false;
  std::map<int, bool> los_launch_scan_in;  // chain_id -> launch-shift scan-in bit
  // When non-empty, only the named clocks are pulsed during the launch+capture
  // window (LOC launch/LOS launch-shift/capture pulses). All clocks are still
  // pulsed during load and unload. Empty = pulse all (regression-safe default).
  std::vector<std::string> active_clock_ports;
};

// Returns true if clock i should be pulsed during the launch/capture window.
// When active_clock_ports is empty every clock is active (unchanged behaviour).
inline bool is_active(const ScanPatternRequest& req, size_t i) {
  if (req.active_clock_ports.empty()) return true;
  if (i >= req.clock_ports.size()) return false;
  const std::string& name = req.clock_ports[i];
  for (const auto& ap : req.active_clock_ports)
    if (ap == name) return true;
  return false;
}

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
