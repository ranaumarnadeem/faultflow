add_executable(
  faultflow_tests
  test_main.cpp
  sim/test_gate_eval.cpp
  sim/test_sky130_gate_eval.cpp
  sim/test_golden_ref.cpp
  sim/test_sequential_ff.cpp
  scan/test_scan_chain.cpp
  scan/test_scan_sequence.cpp
  sim/test_bit_parallel.cpp
  ir/test_parsed_graph.cpp
  ir/test_normalized_graph.cpp
  ir/test_compiled_graph.cpp
  fault/test_enumerator.cpp
  fault/test_batch_manager.cpp
  fault/test_collapser.cpp
  db/test_sqlite_store.cpp
  atpg/test_cnf_truth_tables.cpp
  atpg/test_miter_sat.cpp
  fixtures/test_tiny_fixtures.cpp
  benchmarks/test_c17.cpp
  sim/test_golden_ref_tiny.cpp
  helpers/test_helpers.cpp
)

target_compile_definitions(
  faultflow_tests
  PRIVATE FAULTFLOW_FIXTURE_DIR="${CMAKE_SOURCE_DIR}/tests/cpp/fixtures"
          FAULTFLOW_SOURCE_DIR="${CMAKE_SOURCE_DIR}"
)

target_include_directories(
  faultflow_tests PRIVATE ${CMAKE_SOURCE_DIR}/src/core ${CMAKE_SOURCE_DIR}/tests/cpp
)

target_link_libraries(faultflow_tests PRIVATE faultflow_core Catch2::Catch2WithMain)

include(Catch)
catch_discover_tests(faultflow_tests)
