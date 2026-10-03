#pragma once

#include <algorithm>
#include <exception>
#include <thread>
#include <vector>

namespace faultflow {

// Resolve a requested grading thread count. <= 0 means auto (hardware
// concurrency); the Python layer normally resolves this already, so this is a
// safe floor.
inline int effective_sim_threads(int sim_threads) {
  if (sim_threads > 0) {
    return sim_threads;
  }
  const unsigned hw = std::thread::hardware_concurrency();
  return hw == 0 ? 1 : static_cast<int>(hw);
}

// Run fn(i) for every i in [0, count), on up to `threads` threads (the caller
// included), each taking one contiguous range of indices. fn must only write
// state owned by its index. A range that throws is captured and the
// lowest-range failure is rethrown after every thread has joined.
template <typename Fn>
void parallel_ranges(int count, int threads, Fn&& fn) {
  threads = std::min(threads, count);
  if (threads <= 1) {
    for (int i = 0; i < count; ++i) {
      fn(i);
    }
    return;
  }
  const int base = count / threads;
  const int rem = count % threads;
  std::vector<std::exception_ptr> errors(static_cast<size_t>(threads));
  const auto run_range = [&](int t) {
    try {
      const int lo = t * base + std::min(t, rem);
      const int hi = lo + base + (t < rem ? 1 : 0);
      for (int i = lo; i < hi; ++i) {
        fn(i);
      }
    } catch (...) {
      errors[static_cast<size_t>(t)] = std::current_exception();
    }
  };
  std::vector<std::thread> pool;
  pool.reserve(static_cast<size_t>(threads - 1));
  for (int t = 1; t < threads; ++t) {
    pool.emplace_back(run_range, t);
  }
  run_range(0);
  for (std::thread& th : pool) {
    th.join();
  }
  for (const std::exception_ptr& error : errors) {
    if (error) {
      std::rethrow_exception(error);
    }
  }
}

}  // namespace faultflow
