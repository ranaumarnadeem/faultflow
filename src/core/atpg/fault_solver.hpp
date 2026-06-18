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

// Combined launch+capture key (V1 PIs followed by V2 PIs); used to block an
// already-emitted transition vector pair. Length is 2 * pis.size().
std::string transition_pattern_key(const std::map<std::string, bool>& v1,
                                   const std::map<std::string, bool>& v2,
                                   const std::vector<AtpgPiInfo>& pis);

SatSolveResult solve_stuck_at_fault(const CompiledSimGraph& cg,
                                    const std::vector<AtpgPiInfo>& pis,
                                    const CompactFault& fault,
                                    const SatSolveOptions& options,
                                    std::map<std::string, bool>& out);

// Two-frame combinational (broadside) transition-fault ATPG. Emits a launch
// vector (v1_out) and capture vector (v2_out). `fault.type` selects the
// transition: SA0 = slow-to-rise (good 0->1), SA1 = slow-to-fall (good 1->0).
// SAT => v1_out/v2_out detect the transition; UNSAT => redundant for this model;
// TIMEOUT/UNKNOWN => unresolved (never redundant). blocked_patterns entries, when
// present, are combined V1||V2 keys of length 2*pis.size().
SatSolveResult solve_transition_fault(const CompiledSimGraph& cg,
                                      const std::vector<AtpgPiInfo>& pis,
                                      const CompactFault& fault,
                                      const SatSolveOptions& options,
                                      std::map<std::string, bool>& v1_out,
                                      std::map<std::string, bool>& v2_out);

}  // namespace faultflow::atpg
