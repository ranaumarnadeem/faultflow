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

// solve_stuck_at_fault for an X-mode graph, on the X engine's semantics. Where
// the fault's cone holds no unknown (an x/z bit, CONSTX) it is
// solve_stuck_at_fault. Otherwise a dual-rail CNF -- each net a pair (can be 1,
// can be 0) whose gates are eval_gate_dual's own cubes, an unknown (1,1) --
// finds a vector that detects the fault definitely (SAT). With none, the
// two-valued CNF (an unknown free, the same in both machines) decides: UNSAT
// proves the fault redundant (no value of the unknowns lets any vector show
// it); SAT gives a vector that shows it for some value of them, returned as
// POSSIBLE. The X engine grades every vector.
SatSolveResult solve_stuck_at_fault_x(const CompiledSimGraph& cg,
                                      const std::vector<AtpgPiInfo>& pis,
                                      const CompactFault& fault,
                                      const SatSolveOptions& options,
                                      std::map<std::string, bool>& out);

// Incremental Fan-in Cones (IFC) variant of solve_stuck_at_fault: encodes one
// reached observable's fan-in cone at a time and re-solves under a per-solve
// "differ at >= 1 observed-so-far" constraint, so most testable faults build only
// a fraction of the cone CNF. Verdict-identical to solve_stuck_at_fault when no
// solver limit is hit (under a limit it may return UNKNOWN earlier — conservative).
SatSolveResult solve_stuck_at_fault_incremental(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const CompactFault& fault, const SatSolveOptions& options,
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

// solve_transition_fault on an X-mode graph, as solve_stuck_at_fault_x is
// solve_stuck_at_fault's: with no unknown, loop or bus in the fault's cones the
// two-valued solver; otherwise a dual-rail two-frame CNF finds a pair that
// detects the transition definitely (SAT) -- the edge known at the fault net in
// both frames, a latch holding its launch-frame value in the capture frame. With
// none, the two-valued two-frame CNF (every unknown free) decides: SAT gives a
// pair that shows it for some value of the unknowns (POSSIBLE), UNSAT proves it
// redundant (UNKNOWN through a loop or a bus). The X engine grades every pair.
SatSolveResult solve_transition_fault_x(const CompiledSimGraph& cg,
                                        const std::vector<AtpgPiInfo>& pis,
                                        const CompactFault& fault,
                                        const SatSolveOptions& options,
                                        std::map<std::string, bool>& v1_out,
                                        std::map<std::string, bool>& v2_out);

// Couples one scan FF's capture-frame current state (PPI) to its launch-frame
// next-state (PPO) in the LOC two-frame CNF. Both are CompiledNetIndex values:
// `ppi_compiled` is a member of `pis` (a pseudo-PI input) and `ppo_compiled` is
// a member of cg.observable (a pseudo-PO output).
struct LocCouple {
  uint32_t ppi_compiled = 0;
  uint32_t ppo_compiled = 0;
};

// Scan launch-on-capture (LOC) two-frame transition-fault ATPG on the reduced
// scan ATPG view. Unlike the broadside solver, V2 is FUNCTIONALLY DERIVED from
// V1: per scan FF, the capture-frame PPI is forced equal to the launch-frame PPO
// (`couples`), and every real PI holds launch->capture (`held_pi_compiled`). The
// emitted launch state (v1_out, over real PIs + PPIs) plus the held capture PIs
// fully determine the protocol load. SAT/UNSAT/TIMEOUT/UNKNOWN discipline and the
// combined V1||V2 blocked-pattern key (length 2*pis.size()) match the broadside
// solver.
SatSolveResult solve_scan_transition_fault(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LocCouple>& couples,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out);

// Couples one scan FF's capture-frame current state (PPI) to its chain
// PREDECESSOR's launch-frame current state (PPI) in the LOS two-frame CNF — the
// launch is the last scan shift, so V2[ff] = V1[predecessor(ff)].
struct LosCouple {
  uint32_t capture_ppi_compiled = 0;  // member of `pis`
  uint32_t pred_ppi_compiled = 0;     // member of `pis` (the predecessor)
};

// Scan launch-on-shift (LOS) two-frame transition-fault ATPG on the reduced scan
// ATPG view. The launch transition is created by the last scan shift: per scan
// FF, the capture-frame PPI is forced equal to its chain predecessor's launch
// PPI (`couples`); each chain HEAD PPI (`head_ppi_compiled`) is left FREE (the
// fresh launch scan-in bit); every real PI holds launch->capture
// (`held_pi_compiled`). Because V2 = shift(V1) plus the free head bits, the
// blocked-pattern key is launch ‖ head-bits (length pis.size() + head count),
// not launch-only.
SatSolveResult solve_scan_los_transition_fault(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LosCouple>& couples,
    const std::vector<uint32_t>& head_ppi_compiled,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out);

// The scan LOC and LOS solvers on an X-mode reduced view, as
// solve_transition_fault_x is solve_transition_fault's: a coupled capture net
// reads its launch net's rails (an unknown next state stays unknown). v2_out is
// meaningful for the uncoupled capture inputs (LOS head bits); the scan flows
// derive the rest of the capture from the launch.
SatSolveResult solve_scan_transition_fault_x(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LocCouple>& couples,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out);
SatSolveResult solve_scan_los_transition_fault_x(
    const CompiledSimGraph& cg, const std::vector<AtpgPiInfo>& pis,
    const std::vector<LosCouple>& couples,
    const std::vector<uint32_t>& head_ppi_compiled,
    const std::vector<uint32_t>& held_pi_compiled, const CompactFault& fault,
    const SatSolveOptions& options, std::map<std::string, bool>& v1_out,
    std::map<std::string, bool>& v2_out);

}  // namespace faultflow::atpg
