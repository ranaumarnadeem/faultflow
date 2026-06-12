#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "atpg/sat_atpg.hpp"
#include "fault/effect/compact_fault.hpp"
#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

namespace faultflow::atpg {

struct AtpgPiInfo {
  std::string name;
  int yosys_id = 0;
  uint32_t compiled = 0;
};

std::vector<AtpgPiInfo> ordered_pis(const ParsedGraph& parsed,
                                    const CompiledSimGraph& cg);

std::string pattern_key(const std::map<std::string, bool>& vector,
                        const std::vector<AtpgPiInfo>& pis);

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out);

}  // namespace faultflow::atpg
