add_library(
  faultflow_core
  sim/gate_eval.cpp
  ir/parsed_graph/parsed_graph.cpp
  ir/normalized_graph/normalized_graph.cpp
  ir/normalized_graph/cell_map.cpp
  ir/compiled_graph/graph_compiler.cpp
  fault/enumerator/fault_enumerator.cpp
  fault/collapser/fault_collapser.cpp
  fault/batch/batch_manager.cpp
  db/sqlite_store.cpp
  scan/scan_chain.cpp
  sim/golden_ref/golden_ref_sim.cpp
  sim/engine/bit_parallel_sim.cpp
  sim/state/test_vector.cpp
)

target_include_directories(faultflow_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

set_target_properties(faultflow_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

target_link_libraries(
  faultflow_core PUBLIC nlohmann_json::nlohmann_json SQLiteCpp
)

target_compile_options(faultflow_core PRIVATE -Wall -Wextra)

pybind11_add_module(_faultflow_core bindings/python_bindings.cpp)
target_link_libraries(_faultflow_core PRIVATE faultflow_core)
