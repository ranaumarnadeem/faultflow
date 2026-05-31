#include <iostream>
#include <yaml-cpp/yaml.h>

int main() {
  try {
    YAML::Node root = YAML::LoadFile("cells/osu/osu035.yml");
    std::cout << "map=" << root.IsMap() << " size=" << root.size() << "\n";
    for (auto it = root.begin(); it != root.end(); ++it) {
      std::cout << it->first.as<std::string>() << "\n";
    }
  } catch (const std::exception& e) {
    std::cerr << e.what() << "\n";
    return 1;
  }
}
