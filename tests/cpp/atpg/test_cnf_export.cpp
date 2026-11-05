#include <catch2/catch_test_macros.hpp>

#include <cadical.hpp>
#include <unistd.h>

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "atpg/fault_solver.hpp"
#include "atpg/sat_atpg.hpp"
#include "fault/enumerator/fault_enumerator.hpp"
#include "helpers/test_helpers.hpp"
#include "sim/golden_ref/golden_ref_sim.hpp"
#include "sim/state/test_vector.hpp"

using namespace faultflow;
using namespace faultflow::atpg;

// Per-fault CNF export (SatSolveOptions::cnf_export): the DIMACS file a solve
// writes, read back by a fresh CaDiCaL, gives the solve's own verdict for every
// enumerated fault, and a model of it, read through the exported input
// variables, is a test the scalar reference simulator confirms.

namespace {

SatSolveOptions unlimited(CnfExport* cnf) {
  SatSolveOptions options;
  options.conflict_limit = -1;
  options.sat_timeout_seconds = 0;
  options.cone_restrict = true;
  options.lazy_launch = false;
  options.cnf_export = cnf;
  return options;
}

// The file's own verdict (10 SAT, 20 UNSAT), with its model in `solver`.
int solve_file(CaDiCaL::Solver& solver, const std::string& path) {
  int vars = 0;
  const char* error = solver.read_dimacs(path.c_str(), vars);
  REQUIRE(error == nullptr);
  return solver.solve();
}

TestVector vector_from(const ParsedGraph& pg, CaDiCaL::Solver& solver,
                       const std::vector<std::pair<std::string, int>>& inputs) {
  TestVector vec;
  for (const auto& [name, var] : inputs) {
    // A variable no clause names is free in the file: either value.
    vec.inputs[pg.net_id_by_name(name)] =
        var <= solver.vars() && solver.val(var) > 0;
  }
  return vec;
}

size_t check_export(const std::string& rel, bool transition) {
  const ParsedGraph pg = test::load_parsed_benchmark(rel);
  const NormalizedGraph ng = test::load_normalized_benchmark(rel);
  const CompiledSimGraph cg = test::load_compiled_benchmark(rel);
  const auto pis = ordered_pis(pg, cg);
  const auto faults = transition ? enumerate_transition_faults(ng, cg)
                                 : enumerate_faults(ng, cg);
  // Its own file: ctest runs each test case in a process of its own, at once.
  const std::string path =
      (std::filesystem::temp_directory_path() /
       ("faultflow_cnf_export_" + std::to_string(::getpid()) + ".cnf"))
          .string();
  GoldenRefSim golden;

  size_t sat = 0;
  for (const auto& fault : faults) {
    if (fault.exclusion != FaultExclusion::NONE) {
      continue;
    }
    INFO(rel << " net_index=" << fault.net_index
             << " type=" << static_cast<int>(fault.type));
    CnfExport cnf;
    cnf.path = path;
    std::map<std::string, bool> v1, v2;
    const SatSolveResult verdict =
        transition ? solve_transition_fault(cg, pis, fault, unlimited(&cnf),
                                            v1, v2)
                   : solve_stuck_at_fault(cg, pis, fault, unlimited(&cnf), v1);
    CaDiCaL::Solver solver;
    const int file = solve_file(solver, path);
    REQUIRE(file == (verdict == SatSolveResult::SAT ? 10 : 20));
    if (cnf.structurally_unsat) {
      REQUIRE(cnf.inputs.empty());
      continue;
    }
    REQUIRE(cnf.inputs.size() == pis.size());
    REQUIRE(cnf.launch_inputs.size() == (transition ? pis.size() : 0));
    REQUIRE(cnf.fault_var > 0);
    REQUIRE_FALSE(cnf.diff_vars.empty());
    if (file != 10) {
      continue;
    }
    ++sat;
    // The file says the fault net's faulty copy is stuck, and some
    // observable differs.
    REQUIRE((solver.val(cnf.fault_var) > 0) == (fault.type == FaultType::SA1));
    bool differs = false;
    for (int d : cnf.diff_vars) {
      differs = differs || solver.val(d) > 0;
    }
    REQUIRE(differs);
    const TestVector capture = vector_from(pg, solver, cnf.inputs);
    if (transition) {
      REQUIRE(golden.simulate_transition_fault(
          cg, vector_from(pg, solver, cnf.launch_inputs), capture, fault));
    } else {
      REQUIRE(golden.is_detected(cg, golden.simulate_fault_free(cg, capture),
                                 golden.simulate_with_fault(cg, capture, fault)));
    }
  }
  std::filesystem::remove(path);
  return sat;
}

void check_export(bool transition) {
  size_t sat = 0;
  for (const char* rel : {"iscas85/synth_sky130/c17.json",
                          "iscas85/synth_sky130/c432.json"}) {
    sat += check_export(rel, transition);
  }
  REQUIRE(sat > 0);
}

}  // namespace

TEST_CASE("CNF export: the stuck-at file's verdict and model match the solve",
          "[atpg][cnf_export]") {
  check_export(false);
}

TEST_CASE("CNF export: the transition file's verdict and model match the solve",
          "[atpg][cnf_export][transition]") {
  check_export(true);
}

TEST_CASE("CNF export: the incremental solver refuses it", "[atpg][cnf_export]") {
  const std::string rel = "iscas85/synth_sky130/c17.json";
  const ParsedGraph pg = test::load_parsed_benchmark(rel);
  const NormalizedGraph ng = test::load_normalized_benchmark(rel);
  const CompiledSimGraph cg = test::load_compiled_benchmark(rel);
  const auto pis = ordered_pis(pg, cg);
  const auto faults = enumerate_faults(ng, cg);
  CnfExport cnf;
  cnf.path = "unused.cnf";
  SatSolveOptions options = unlimited(&cnf);
  options.incremental = true;
  std::map<std::string, bool> vector;
  REQUIRE_THROWS(
      solve_stuck_at_fault_incremental(cg, pis, faults.front(), options, vector));
}
