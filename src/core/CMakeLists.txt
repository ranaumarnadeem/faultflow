add_library(
  faultflow_core
  sim/gate_eval.cpp
  ir/parsed_graph/parsed_graph.cpp
  ir/normalized_graph/normalized_graph.cpp
  ir/normalized_graph/cell_map.cpp
  ir/compiled_graph/graph_compiler.cpp
  fault/enumerator/fault_enumerator.cpp
  fault/batch/batch_manager.cpp
  sim/golden_ref/golden_ref_sim.cpp
  sim/engine/bit_parallel_sim.cpp
  sim/state/test_vector.cpp
)

target_include_directories(faultflow_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(faultflow_core PUBLIC nlohmann_json::nlohmann_json)

target_compile_options(faultflow_core PRIVATE -Wall -Wextra)
