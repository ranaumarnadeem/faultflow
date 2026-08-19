#include <catch2/catch_test_macros.hpp>

#include "scan/compression.hpp"

using namespace faultflow;
using namespace faultflow::scan;

namespace {

// Recomputes every internal bit from a channel assignment (internal_bit[i] = XOR of the
// channels listed in map.fanout[i]) and checks it against each entry in care_bits. Used to
// independently verify solve_xor_broadcast's output rather than trusting the solver's own
// bookkeeping.
bool assignment_satisfies(const XorBroadcastMap& map, const std::vector<CareBit>& care_bits,
                           const std::vector<bool>& channels) {
  for (const CareBit& cb : care_bits) {
    bool bit = false;
    for (int ch : map.fanout.at(static_cast<size_t>(cb.internal_index))) {
      bit ^= channels.at(static_cast<size_t>(ch));
    }
    if (bit != cb.value) return false;
  }
  return true;
}

}  // namespace

TEST_CASE("Identity map: each internal bit is exactly one channel", "[scan_compression]") {
  // GIVEN a 3-channel, 3-chain map where internal bit i is driven by channel i alone
  XorBroadcastMap map;
  map.num_channels = 3;
  map.fanout = {{0}, {1}, {2}};

  // WHEN solving for a fully-specified care-bit pattern
  std::vector<CareBit> care = {{0, true}, {1, false}, {2, true}};
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN it finds the exact matching assignment
  REQUIRE(ok);
  REQUIRE(channels.size() == 3);
  REQUIRE(channels[0] == true);
  REQUIRE(channels[1] == false);
  REQUIRE(channels[2] == true);
}

TEST_CASE("XOR-broadcast map: consistent care bits are satisfiable", "[scan_compression]") {
  // GIVEN 2 channels feeding 3 internal bits: bit0=c0, bit1=c1, bit2=c0^c1
  XorBroadcastMap map;
  map.num_channels = 2;
  map.fanout = {{0}, {1}, {0, 1}};

  // WHEN the care bits are internally consistent (1 ^ 0 == 1)
  std::vector<CareBit> care = {{0, true}, {1, false}, {2, true}};
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN a satisfying assignment is found and independently verified
  REQUIRE(ok);
  REQUIRE(channels.size() == 2);
  REQUIRE(assignment_satisfies(map, care, channels));
}

TEST_CASE("XOR-broadcast map: inconsistent care bits are rejected", "[scan_compression]") {
  // GIVEN the same 2-channel/3-bit map as above
  XorBroadcastMap map;
  map.num_channels = 2;
  map.fanout = {{0}, {1}, {0, 1}};

  // WHEN the care bits contradict the map's linear structure (1 ^ 0 != 0)
  std::vector<CareBit> care = {{0, true}, {1, false}, {2, false}};
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN the solver correctly reports unsatisfiable
  REQUIRE_FALSE(ok);
}

TEST_CASE("XOR-broadcast map: no care bits is trivially satisfiable", "[scan_compression]") {
  // GIVEN any map
  XorBroadcastMap map;
  map.num_channels = 4;
  map.fanout = {{0}, {1}, {2}, {3}, {0, 1, 2, 3}};

  // WHEN there are no care-bit requirements at all
  std::vector<CareBit> care;
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN it's trivially satisfiable, returning a fully-sized (all-false by convention) assignment
  REQUIRE(ok);
  REQUIRE(channels.size() == 4);
}

TEST_CASE("XOR-broadcast map: duplicate consistent care bits are fine", "[scan_compression]") {
  // GIVEN a map where internal bit 0 is checked twice with the same required value
  XorBroadcastMap map;
  map.num_channels = 2;
  map.fanout = {{0}, {1}};
  std::vector<CareBit> care = {{0, true}, {0, true}, {1, false}};

  // WHEN solving
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN it's satisfiable (duplicates that agree don't conflict)
  REQUIRE(ok);
  REQUIRE(assignment_satisfies(map, care, channels));
}

TEST_CASE("XOR-broadcast map: duplicate conflicting care bits are rejected",
          "[scan_compression]") {
  // GIVEN a map where internal bit 0 is required to be both true and false
  XorBroadcastMap map;
  map.num_channels = 2;
  map.fanout = {{0}, {1}};
  std::vector<CareBit> care = {{0, true}, {0, false}};

  // WHEN solving
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN it's correctly rejected
  REQUIRE_FALSE(ok);
}

TEST_CASE("XOR-broadcast map: sparse care bits with free channels", "[scan_compression]") {
  // GIVEN 4 channels feeding 8 internal chain bits via a rotating 2-input XOR pattern
  // (bit i = channel i%4 XOR channel (i+1)%4) -- structurally similar to a small real
  // decompressor, with more internal bits than channels
  XorBroadcastMap map;
  map.num_channels = 4;
  map.fanout.resize(8);
  for (int i = 0; i < 8; ++i) {
    map.fanout[static_cast<size_t>(i)] = {i % 4, (i + 1) % 4};
  }

  // WHEN only a sparse subset of bits carry a requirement (matching how ATPG patterns are
  // mostly don't-care) and the requirement is reachable
  std::vector<CareBit> care = {{0, true}, {3, false}, {5, true}};
  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN a satisfying assignment exists and is independently verified against every care bit,
  // including the ones not directly pinned (channels not touched by any care bit are free and
  // may be set to anything by the solver, but the ones that ARE constrained must hold)
  REQUIRE(ok);
  REQUIRE(channels.size() == 4);
  REQUIRE(assignment_satisfies(map, care, channels));
}

TEST_CASE("XOR-broadcast map: sparse care bits that are unreachable together",
          "[scan_compression]") {
  // GIVEN the same rotating 4-channel/8-bit map
  XorBroadcastMap map;
  map.num_channels = 4;
  map.fanout.resize(8);
  for (int i = 0; i < 8; ++i) {
    map.fanout[static_cast<size_t>(i)] = {i % 4, (i + 1) % 4};
  }

  // WHEN the care-bit set is large enough to over-constrain the 4 free channels into a
  // contradiction (bits 0,1,2,3 pin every channel individually via XOR pairs, then bit 4
  // -- which is channel0^channel1 again, same pair as bit0 -- is required to disagree with
  // what bits 0 and 1 already force)
  std::vector<CareBit> care = {
      {0, true},   // c0 ^ c1 = true
      {1, false},  // c1 ^ c2 = false  => c2 = c1
      {2, true},   // c2 ^ c3 = true   => c3 = ~c2
      {3, false},  // c3 ^ c0 = false  => c0 = c3
  };
  // These four alone are consistent (a 4-cycle of XOR constraints with an even number of
  // "true" edges is satisfiable). Add one more that breaks it: bit 0 again, contradicting.
  care.push_back({0, false});

  std::vector<bool> channels;
  bool ok = solve_xor_broadcast(map, care, channels);

  // THEN the solver detects the contradiction
  REQUIRE_FALSE(ok);
}
