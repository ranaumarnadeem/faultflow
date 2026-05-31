#include "fault/batch/batch_manager.hpp"

namespace faultflow {

BatchManager::BatchManager(std::vector<CompactFault> faults) {
  pending_.reserve(faults.size());
  for (auto& f : faults) {
    if (f.exclusion == FaultExclusion::NONE) {
      pending_.push_back(std::move(f));
    }
  }
}

bool BatchManager::has_pending() const {
  return cursor_ < pending_.size();
}

FaultBatch BatchManager::next_batch() {
  FaultBatch batch;
  batch.size = 0;
  batch.mask = 0;
  while (batch.size < BATCH_SIZE && cursor_ < pending_.size()) {
    CompactFault& f = pending_[cursor_++];
    if (f.status != FaultStatus::PENDING) {
      continue;
    }
    f.bit = static_cast<uint8_t>(batch.size + 1);
    f.sa_mask = 1ULL << f.bit;
    batch.faults[batch.size++] = f;
    batch.mask |= f.sa_mask;
  }
  return batch;
}

void BatchManager::mark_detected(const FaultBatch& batch,
                                 uint64_t detected_mask) {
  for (int i = 0; i < batch.size; ++i) {
    if ((detected_mask & batch.faults[i].sa_mask) == 0) {
      continue;
    }
    const CompactFault& hit = batch.faults[i];
    for (auto& pf : pending_) {
      if (pf.net_index == hit.net_index && pf.type == hit.type &&
          pf.bit == hit.bit) {
        pf.status = FaultStatus::DETECTED;
        detected_.push_back(pf);
        break;
      }
    }
  }
}

void BatchManager::drop_detected() {
  std::vector<CompactFault> still_pending;
  still_pending.reserve(pending_.size());
  for (const auto& f : pending_) {
    if (f.status == FaultStatus::PENDING) {
      still_pending.push_back(f);
    }
  }
  pending_ = std::move(still_pending);
  cursor_ = 0;
}

void BatchManager::finish_batch(const FaultBatch& batch, uint64_t detected_mask) {
  mark_detected(batch, detected_mask);
  for (int i = 0; i < batch.size; ++i) {
    if ((detected_mask & batch.faults[i].sa_mask) != 0) {
      continue;
    }
    const CompactFault& miss = batch.faults[i];
    for (auto& pf : pending_) {
      if (pf.net_index == miss.net_index && pf.type == miss.type &&
          pf.bit == miss.bit) {
        pf.status = FaultStatus::UNDETECTED;
        break;
      }
    }
  }
  std::vector<CompactFault> still_pending;
  still_pending.reserve(pending_.size());
  for (const auto& f : pending_) {
    if (f.status == FaultStatus::PENDING) {
      still_pending.push_back(f);
    }
  }
  pending_ = std::move(still_pending);
  cursor_ = 0;
}

}  // namespace faultflow
