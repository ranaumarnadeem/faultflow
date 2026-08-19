#pragma once

#include <vector>

namespace faultflow::scan {

// A fixed linear (XOR-based) scan-compression decompressor: expands K external test
// channels into N internal scan-chain-input bits via internal_bit[i] = XOR of the
// external channels listed in fanout[i]. Purely combinational/static -- no LFSR state.
struct XorBroadcastMap {
  int num_channels = 0;
  std::vector<std::vector<int>> fanout;  // fanout[i] = channel indices feeding internal bit i
};

struct CareBit {
  int internal_index = 0;
  bool value = false;
};

// Determines whether an assignment of the map's external channels exists that satisfies
// every entry in care_bits, and if so, produces ONE such assignment in out_channels (sized
// to map.num_channels; free/unconstrained channels are set to false). Returns false (leaving
// out_channels unspecified) if the care-bit requirements are inconsistent under this map.
bool solve_xor_broadcast(const XorBroadcastMap& map,
                          const std::vector<CareBit>& care_bits,
                          std::vector<bool>& out_channels);

}  // namespace faultflow::scan
