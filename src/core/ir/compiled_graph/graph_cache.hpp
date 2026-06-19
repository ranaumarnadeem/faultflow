#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "ir/compiled_graph/compiled_graph.hpp"
#include "ir/normalized_graph/normalized_graph.hpp"
#include "ir/parsed_graph/parsed_graph.hpp"

namespace faultflow {

// Bundles the three IR layers built from one netlist input. Immutable after
// construction and consumed read-only by simulation/ATPG, so a single instance
// can back many fault solves without rebuilding.
struct CachedGraph {
  ParsedGraph parsed;
  NormalizedGraph ng;
  CompiledSimGraph cg;
};

struct GraphCacheStats {
  int64_t builds = 0;  // actual parse->normalize->compile runs (cache misses)
  int64_t hits = 0;    // reuses of an already-built graph
};

// Returns a reference to a process-global cached graph for the given inputs,
// building (parse -> normalize -> compile) once per distinct input and reusing
// it thereafter. The returned reference stays valid for the process lifetime
// (or until clear_graph_cache()) because the backing std::map keeps node
// addresses stable across inserts.
//
// The cache key includes the size and mtime of both the netlist JSON and the
// cell-map file, so an edit to either input within a single process forces a
// rebuild; no external invalidation hook is required.
const CachedGraph& load_cached_graph(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances);

// 3-arg convenience overload (no blackbox). A named static empty list avoids a
// temporary at the call site (which would trip GCC's -Wdangling-reference
// heuristic on the returned reference, even though it points into the cache).
inline const CachedGraph& load_cached_graph(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& unsupported_policy) {
  static const std::vector<std::string> kNoBlackbox;
  return load_cached_graph(json_path, cell_map_path, unsupported_policy,
                           kNoBlackbox);
}

GraphCacheStats graph_cache_stats();

// Drops all cached graphs and zeroes the stats. Production never calls this
// (the cache should persist for the whole run); it exists so tests can force a
// deterministic cold start.
void clear_graph_cache();

}  // namespace faultflow
