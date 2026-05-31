#include "helpers/test_helpers.hpp"

#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/cell_map.hpp"

namespace faultflow::test {

std::string fixture_path(const std::string& name) {
  return std::string(FAULTFLOW_FIXTURE_DIR) + "/" + name;
}

std::string cell_map_path() {
  return std::string(FAULTFLOW_SOURCE_DIR) + "/cells/osu/osu035.json";
}

ParsedGraph load_parsed(const std::string& fixture_name) {
  return ParsedGraph::from_file(fixture_path(fixture_name));
}

NormalizedGraph load_normalized(const std::string& fixture_name) {
  const ParsedGraph pg = load_parsed(fixture_name);
  const CellMap map = CellMap::load(cell_map_path());
  return NormalizedGraph::from_parsed(pg, map);
}

CompiledSimGraph load_compiled(const std::string& fixture_name) {
  const NormalizedGraph ng = load_normalized(fixture_name);
  return GraphCompiler::compile(ng);
}

}  // namespace faultflow::test
