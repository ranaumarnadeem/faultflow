#include "scan/compression.hpp"

#include <cstddef>
#include <utility>

namespace faultflow::scan {

bool solve_xor_broadcast(const XorBroadcastMap& map,
                          const std::vector<CareBit>& care_bits,
                          std::vector<bool>& out_channels) {
  const size_t k = static_cast<size_t>(map.num_channels);

  // One augmented row per care bit: k coefficient columns (does this care bit's XOR
  // fanout touch channel c?) plus one RHS column (the required value).
  std::vector<std::vector<bool>> rows;
  rows.reserve(care_bits.size());
  for (const CareBit& cb : care_bits) {
    std::vector<bool> row(k + 1, false);
    for (int ch : map.fanout.at(static_cast<size_t>(cb.internal_index))) {
      row[static_cast<size_t>(ch)] = !row[static_cast<size_t>(ch)];
    }
    row[k] = cb.value;
    rows.push_back(std::move(row));
  }

  // Gauss-Jordan elimination over GF(2): reduce to reduced row-echelon form directly (each
  // pivot is eliminated from every OTHER row, above and below), so no separate
  // back-substitution pass is needed afterward.
  std::vector<int> pivot_col_for_row(rows.size(), -1);
  size_t pivot_row = 0;
  for (size_t col = 0; col < k && pivot_row < rows.size(); ++col) {
    size_t sel = pivot_row;
    while (sel < rows.size() && !rows[sel][col]) {
      ++sel;
    }
    if (sel == rows.size()) {
      continue;  // no unpivoted row has a 1 in this column; move on
    }
    std::swap(rows[pivot_row], rows[sel]);
    for (size_t r = 0; r < rows.size(); ++r) {
      if (r != pivot_row && rows[r][col]) {
        for (size_t c = col; c <= k; ++c) {
          rows[r][c] = (rows[r][c] != rows[pivot_row][c]);
        }
      }
    }
    pivot_col_for_row[pivot_row] = static_cast<int>(col);
    ++pivot_row;
  }

  // Any row left with an all-zero coefficient side but a required-true RHS is a contradiction
  // -- the care-bit requirements are not simultaneously reachable through this map.
  for (size_t r = pivot_row; r < rows.size(); ++r) {
    bool all_zero_coeffs = true;
    for (size_t c = 0; c < k; ++c) {
      if (rows[r][c]) {
        all_zero_coeffs = false;
        break;
      }
    }
    if (all_zero_coeffs && rows[r][k]) {
      return false;
    }
  }

  // Free (never-pivoted) channels default to false; pivoted channels take their row's RHS.
  out_channels.assign(k, false);
  for (size_t r = 0; r < pivot_row; ++r) {
    out_channels[static_cast<size_t>(pivot_col_for_row[r])] = rows[r][k];
  }
  return true;
}

}  // namespace faultflow::scan
