#include "ir/parsed_graph/parsed_graph.hpp"

#include <fstream>

#include "common/errors.hpp"
#include "common/types.hpp"

namespace faultflow {
namespace {

int parse_bit(const nlohmann::json& b) {
  if (b.is_number_integer()) {
    return b.get<int>();
  }
  if (b.is_string()) {
    const std::string s = b.get<std::string>();
    if (s == "0") {
      return CONST0_NET_ID;
    }
    if (s == "1") {
      return CONST1_NET_ID;
    }
    if (s == "x" || s == "z") {
      throw ParseError("X/Z constants not supported in Phase 0");
    }
    throw ParseError("Unknown constant bit: " + s);
  }
  throw ParseError("Invalid bit entry in JSON");
}

std::vector<int> parse_bits(const nlohmann::json& arr) {
  std::vector<int> out;
  if (!arr.is_array()) {
    return out;
  }
  for (const auto& b : arr) {
    out.push_back(parse_bit(b));
  }
  return out;
}

}  // namespace

ParsedGraph ParsedGraph::from_json(const nlohmann::json& j) {
  ParsedGraph g;
  if (!j.contains("modules") || !j["modules"].is_object()) {
    throw ParseError("JSON missing modules object");
  }

  for (auto it = j["modules"].begin(); it != j["modules"].end(); ++it) {
    const std::string mod_name = it.key();
    const auto& mod_j = it.value();
    ParsedModule mod;

    if (mod_j.contains("attributes") && mod_j["attributes"].is_object()) {
      for (auto a = mod_j["attributes"].begin(); a != mod_j["attributes"].end();
           ++a) {
        mod.attrs[a.key()] = a.value().get<std::string>();
      }
    }

    if (mod.attrs.count("blackbox") && mod.attrs.at("blackbox") == "1") {
      g.lib_cells.insert(mod_name);
      continue;
    }

    if (mod.attrs.count("top") && mod.attrs.at("top") == "1") {
      if (!g.top.empty()) {
        throw ParseError("Multiple top modules");
      }
      g.top = mod_name;
    }

    if (mod_j.contains("ports") && mod_j["ports"].is_object()) {
      for (auto p = mod_j["ports"].begin(); p != mod_j["ports"].end(); ++p) {
        ParsedPort port;
        port.direction = p.value().value("direction", "");
        port.bits = parse_bits(p.value()["bits"]);
        mod.ports[p.key()] = std::move(port);
      }
    }

    if (mod_j.contains("cells") && mod_j["cells"].is_object()) {
      for (auto c = mod_j["cells"].begin(); c != mod_j["cells"].end(); ++c) {
        ParsedCell cell;
        cell.instance = c.key();
        cell.type = c.value().value("type", "");
        if (c.value().contains("connections")) {
          for (auto conn = c.value()["connections"].begin();
               conn != c.value()["connections"].end(); ++conn) {
            cell.conns[conn.key()] = parse_bits(conn.value());
          }
        }
        mod.cells[cell.instance] = std::move(cell);
      }
    }

    if (mod_j.contains("netnames") && mod_j["netnames"].is_object()) {
      for (auto n = mod_j["netnames"].begin(); n != mod_j["netnames"].end();
           ++n) {
        ParsedNet net;
        net.name = n.key();
        net.bits = parse_bits(n.value()["bits"]);
        net.hide = n.value().value("hide", false);
        mod.netnames[net.name] = std::move(net);
      }
    }

    g.modules[mod_name] = std::move(mod);
  }

  if (g.top.empty()) {
    throw ParseError("No top module (top=1 attribute)");
  }
  return g;
}

ParsedGraph ParsedGraph::from_json_string(const std::string& s) {
  try {
    return from_json(nlohmann::json::parse(s));
  } catch (const nlohmann::json::exception& e) {
    throw ParseError(std::string("Malformed JSON: ") + e.what());
  }
}

ParsedGraph ParsedGraph::from_file(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw ParseError("Cannot open file: " + path);
  }
  try {
    nlohmann::json j;
    in >> j;
    return from_json(j);
  } catch (const nlohmann::json::exception& e) {
    throw ParseError(std::string("Malformed JSON: ") + e.what());
  }
}

const ParsedModule& ParsedGraph::top_module() const {
  return modules.at(top);
}

}  // namespace faultflow
