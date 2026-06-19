#include "ir/compiled_graph/graph_cache.hpp"

#include <cstdint>
#include <filesystem>
#include <map>
#include <mutex>
#include <set>
#include <system_error>
#include <utility>

#include "ir/normalized_graph/cell_map.hpp"

namespace faultflow {
namespace {

std::mutex g_cache_mutex;
std::map<std::string, CachedGraph> g_cache;
GraphCacheStats g_stats;

// Appends a size+mtime fingerprint of `path` to `key`. Uses the error_code
// overloads so a missing/unreadable file contributes a stable sentinel here
// rather than throwing; the subsequent build surfaces the real load error.
void append_file_fingerprint(std::string& key, const std::string& path) {
  std::error_code size_ec;
  const std::uintmax_t size = std::filesystem::file_size(path, size_ec);
  std::error_code time_ec;
  const auto mtime = std::filesystem::last_write_time(path, time_ec);
  const std::int64_t mtime_ticks =
      time_ec ? std::int64_t{-1}
              : static_cast<std::int64_t>(mtime.time_since_epoch().count());
  key += '\0';
  key += size_ec ? "?" : std::to_string(size);
  key += '\0';
  key += std::to_string(mtime_ticks);
}

std::string make_key(const std::string& json_path,
                     const std::string& cell_map_path,
                     const std::string& unsupported_policy,
                     const std::vector<std::string>& blackbox_instances) {
  std::string key = json_path;
  key += '\0';
  key += cell_map_path;
  key += '\0';
  key += unsupported_policy;
  // Blackbox instances change normalization, so they must key the cache.
  // Sort for order-independence.
  std::set<std::string> bb(blackbox_instances.begin(),
                           blackbox_instances.end());
  for (const auto& name : bb) {
    key += '\0';
    key += name;
  }
  append_file_fingerprint(key, json_path);
  append_file_fingerprint(key, cell_map_path);
  return key;
}

CachedGraph build_graph(const std::string& json_path,
                        const std::string& cell_map_path,
                        const std::string& unsupported_policy,
                        const std::vector<std::string>& blackbox_instances) {
  CachedGraph graph;
  graph.parsed = ParsedGraph::from_file(json_path);
  const CellMap cell_map = CellMap::load(cell_map_path);
  const std::set<std::string> bb(blackbox_instances.begin(),
                                 blackbox_instances.end());
  graph.ng = NormalizedGraph::from_parsed(graph.parsed, cell_map,
                                          unsupported_policy, bb);
  graph.cg = GraphCompiler::compile(graph.ng);
  return graph;
}

}  // namespace

const CachedGraph& load_cached_graph(
    const std::string& json_path, const std::string& cell_map_path,
    const std::string& unsupported_policy,
    const std::vector<std::string>& blackbox_instances) {
  // One lock guards the whole find-or-build. The runtime is single-threaded
  // today (the pybind layer never releases the GIL), so contention is nil; the
  // lock is defensive. If any binding later releases the GIL, removing it would
  // be a data race on g_cache. std::map node stability keeps the returned
  // reference valid after the lock is released and across later inserts.
  std::lock_guard<std::mutex> lock(g_cache_mutex);
  const std::string key =
      make_key(json_path, cell_map_path, unsupported_policy, blackbox_instances);
  const auto it = g_cache.find(key);
  if (it != g_cache.end()) {
    ++g_stats.hits;
    return it->second;
  }
  // build_graph may throw (missing file, unsupported cell): the lock releases
  // and nothing is inserted, so a failed build neither counts nor poisons the
  // cache.
  CachedGraph graph = build_graph(json_path, cell_map_path, unsupported_policy,
                                  blackbox_instances);
  ++g_stats.builds;
  return g_cache.emplace(std::move(key), std::move(graph)).first->second;
}

GraphCacheStats graph_cache_stats() {
  std::lock_guard<std::mutex> lock(g_cache_mutex);
  return g_stats;
}

void clear_graph_cache() {
  std::lock_guard<std::mutex> lock(g_cache_mutex);
  g_cache.clear();
  g_stats = {};
}

}  // namespace faultflow
