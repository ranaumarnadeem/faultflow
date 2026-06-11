#pragma once

#include <map>
#include <string>
#include <vector>

namespace faultflow::atpg {

struct SatAtpgOptions {
  int random_vectors = 64;
  int conflict_limit = 100000;
  int max_sat_vectors = 10000;
  bool include_clock_faults = false;
  bool include_reset_faults = false;
  bool collapsing = false;
};

std::vector<std::map<std::string, bool>> generate_comb_sat_vectors(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& unsupported_policy, const SatAtpgOptions& options);

}  // namespace faultflow::atpg
