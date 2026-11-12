#pragma once

#include <stdexcept>
#include <string>

namespace faultflow {

class ParseError : public std::runtime_error {
 public:
  explicit ParseError(const std::string& msg) : std::runtime_error(msg) {}
};

class UnsupportedCellError : public std::runtime_error {
 public:
  explicit UnsupportedCellError(const std::string& cell)
      : std::runtime_error("Unsupported cell: " + cell), cell_(cell) {}
  const std::string& cell() const { return cell_; }

 private:
  std::string cell_;
};

// A combinational loop in a netlist a two-valued graph is built for: its value is
// not a function of the inputs, so only the X engine ([simulation] engine = x)
// simulates it.
class CombinationalLoopError : public std::runtime_error {
 public:
  explicit CombinationalLoopError(const std::string& msg) : std::runtime_error(msg) {}
};

}  // namespace faultflow
