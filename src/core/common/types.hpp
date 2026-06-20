#pragma once

#include <cstdint>
#include <limits>

namespace faultflow {

using YosysNetID = int;
using CompiledNetIndex = uint32_t;

inline constexpr YosysNetID CONST0_NET_ID = -1;
inline constexpr YosysNetID CONST1_NET_ID = -2;

inline constexpr CompiledNetIndex UNUSED_INPUT =
    std::numeric_limits<CompiledNetIndex>::max();

enum class GateType {
  INV,
  BUF,
  AND2,
  AND2B,
  AND3,
  AND4,
  OR2,
  OR3,
  OR4,
  NAND2,
  NAND3,
  NAND4,
  NOR2,
  NOR3,
  NOR4,
  XOR2,
  XOR3,
  XNOR2,
  XNOR3,
  AOI21,
  AOI22,
  OAI21,
  OAI22,
  MUX2,
  MUX2_NI,
  A21O,
  A21OI,
  A21BO,
  A21BOI,
  A22O,
  A22OI,
  A211OI,
  A2111OI,
  A221O,
  A221OI,
  A31O,
  A31OI,
  A32O,
  A32OI,
  A41OI,
  O21A,
  O21AI,
  O21BAI,
  O22A,
  O22AI,
  O211AI,
  O221AI,
  O31A,
  O31AI,
  O311A,
  O311AI,
  O32AI,
  NAND2B,
  NAND3B,
  NAND4B,
  NOR2B,
  NOR3B,
  NOR4B,
  OR3B,
  OR4B,
  ADDF_S,
  ADDF_CO,
  ADDH_S,
  ADDH_CO,
  CONST0,
  CONST1,
  INPUT,
  DFF,
  // IEEE 1500 wrapper boundary cells. WBR_IN sits on a core input (drives the
  // core-side net); WBR_OUT sits on a core output (drives the system-side net).
  // In FUNCTIONAL mode both are plain buffers; INTEST/EXTEST reconfigure them
  // into control/observe points (see TestMode + build_mode_config).
  WBR_IN,
  WBR_OUT,
};

enum class NodeType { GATE, FF, LATCH, CONST, TBUF, ICG };

// IEEE 1500 SerialWBR test mode. FUNCTIONAL = wrapper cells are transparent;
// INTEST = test the core internals; EXTEST = test the interconnect around the
// core. Carried on SimState; reconfigures the control/observe point sets.
enum class TestMode : uint8_t { FUNCTIONAL = 0, INTEST = 1, EXTEST = 2 };

enum class TriggerType : uint8_t { POSEDGE = 0, NEGEDGE = 1 };

enum class Polarity : uint8_t { ACTIVE_HIGH = 0, ACTIVE_LOW = 1 };

struct FFControlConfig {
  int net = -1;
  Polarity polarity = Polarity::ACTIVE_HIGH;
  uint8_t value = 0;
  bool present = false;
};

struct FFConfig {
  TriggerType trigger = TriggerType::POSEDGE;
  int clock_net = -1;
  int data_net = -1;
  int output_net = -1;
  int scan_in_net = -1;
  int scan_enable_net = -1;
  FFControlConfig clear;
  FFControlConfig preset;
  Polarity scan_enable_polarity = Polarity::ACTIVE_HIGH;
  bool has_scan = false;
  uint8_t clear_preset_conflict_value = 0;
};

struct CompiledFFConfig {
  TriggerType trigger = TriggerType::POSEDGE;
  Polarity clear_polarity = Polarity::ACTIVE_HIGH;
  Polarity preset_polarity = Polarity::ACTIVE_HIGH;
  uint8_t clear_value = 0;
  uint8_t preset_value = 1;
  uint8_t clear_preset_conflict_value = 0;
  bool has_clear = false;
  bool has_preset = false;
  bool has_scan = false;
  Polarity scan_enable_polarity = Polarity::ACTIVE_HIGH;
};

enum class FaultType : uint8_t { SA0 = 0, SA1 = 1 };

enum class FaultStatus : uint8_t {
  PENDING = 0,
  DETECTED = 1,
  UNDETECTED = 2,
  REDUNDANT = 3,
};

enum class FaultExclusion : uint8_t {
  NONE = 0,
  CLOCK = 1,
  RESET = 2,
  BLACKBOX = 3,
};

// STUCK_AT: single-frame SA0/SA1 (default, existing behaviour unchanged).
// TRANSITION: two-frame STR/STF — type=SA0 means slow-to-rise (capture-frame SA0),
//             type=SA1 means slow-to-fall (capture-frame SA1).
enum class FaultModel : uint8_t { STUCK_AT = 0, TRANSITION = 1 };

}  // namespace faultflow
