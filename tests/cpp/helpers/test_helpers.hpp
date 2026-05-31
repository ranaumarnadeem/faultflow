#pragma once

#include <string>

#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

namespace faultflow::test {

std::string fixture_path(const std::string& name);

ParsedGraph load_parsed(const std::string& fixture_name);

NormalizedGraph load_normalized(const std::string& fixture_name);

CompiledSimGraph load_compiled(const std::string& fixture_name);

std::string cell_map_path();

}  // namespace faultflow::test
