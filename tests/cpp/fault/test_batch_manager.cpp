#include <catch2/catch_test_macros.hpp>

#include "fault/batch/batch_manager.hpp"
#include "fault/effect/compact_fault.hpp"

using namespace faultflow;

TEST_CASE("BatchManager packing boundary", "[batch_manager]") {
  std::vector<CompactFault> faults;
  for (int i = 0; i < 65; ++i) {
    CompactFault f;
    f.net_index = static_cast<uint32_t>(i);
    f.type = (i % 2 == 0) ? FaultType::SA0 : FaultType::SA1;
    faults.push_back(f);
  }
  BatchManager bm(std::move(faults));
  REQUIRE(bm.pending_count() == 65);
  FaultBatch b1 = bm.next_batch();
  REQUIRE(b1.size == 63);
  FaultBatch b2 = bm.next_batch();
  REQUIRE(b2.size == 2);
  REQUIRE_FALSE(bm.has_pending());
}

TEST_CASE("BatchManager drop detected", "[batch_manager]") {
  std::vector<CompactFault> faults(4);
  for (size_t i = 0; i < faults.size(); ++i) {
    faults[i].net_index = static_cast<uint32_t>(i);
  }
  BatchManager bm(std::move(faults));
  FaultBatch batch = bm.next_batch();
  REQUIRE(batch.size == 4);
  bm.mark_detected(batch, batch.faults[0].sa_mask | batch.faults[1].sa_mask);
  bm.drop_detected();
  REQUIRE(bm.pending_count() == 2);
  REQUIRE(bm.detected_count() == 2);
}
