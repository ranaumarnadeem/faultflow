#pragma once

#include <map>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace faultflow {

struct ParsedPort {
  std::string direction;
  std::vector<int> bits;
};

struct ParsedCell {
  std::string instance;
  std::string type;
  std::map<std::string, std::vector<int>> conns;
  std::map<std::string, std::string> attrs;
};

struct ParsedNet {
  std::string name;
  std::vector<int> bits;
  bool hide = false;
};

struct ParsedModule {
  std::map<std::string, ParsedPort> ports;
  std::map<std::string, ParsedCell> cells;
  std::map<std::string, ParsedNet> netnames;
  std::map<std::string, std::string> attrs;
};

struct ParsedGraph {
  std::map<std::string, ParsedModule> modules;
  std::string top;
  std::set<std::string> lib_cells;

  static ParsedGraph from_json(const nlohmann::json& j);
  static ParsedGraph from_json_string(const std::string& s);
  static ParsedGraph from_file(const std::string& path);

  const ParsedModule& top_module() const;
};

}  // namespace faultflow
