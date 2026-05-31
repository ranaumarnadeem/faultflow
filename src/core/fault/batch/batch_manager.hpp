#pragma once

#include <cstdint>
#include <vector>

#include "fault/effect/compact_fault.hpp"
#include "fault/effect/fault_batch.hpp"

namespace faultflow {

class BatchManager {
 public:
  static constexpr int BATCH_SIZE = kBatchSize;

  explicit BatchManager(std::vector<CompactFault> faults);

  bool has_pending() const;
  FaultBatch next_batch();
  void mark_detected(const FaultBatch& batch, uint64_t detected_mask);
  void drop_detected();

  // Mark undetected faults in the batch and remove finished faults from pending.
  void finish_batch(const FaultBatch& batch, uint64_t detected_mask);

  size_t pending_count() const { return pending_.size(); }
  size_t detected_count() const { return detected_.size(); }

 private:
  std::vector<CompactFault> pending_;
  std::vector<CompactFault> detected_;
  size_t cursor_ = 0;
};

}  // namespace faultflow
