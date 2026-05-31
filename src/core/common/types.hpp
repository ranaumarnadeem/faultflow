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
  XNOR2,
  AOI21,
  AOI22,
  AOI211,
  AOI221,
  AOI222,
  OAI21,
  OAI22,
  OAI211,
  OAI221,
  OAI222,
  MUX2,
  MUX4,
  ADDF_S,
  ADDF_CO,
  ADDH_S,
  ADDH_CO,
  CONST0,
  CONST1,
  INPUT,
};

enum class NodeType { GATE, FF, LATCH, CONST, TBUF, ICG };

enum class FaultType : uint8_t { SA0 = 0, SA1 = 1 };

enum class FaultStatus : uint8_t {
  PENDING = 0,
  DETECTED = 1,
  UNDETECTED = 2,
};

enum class FaultExclusion : uint8_t {
  NONE = 0,
  CLOCK = 1,
  RESET = 2,
  BLACKBOX = 3,
};

}  // namespace faultflow
