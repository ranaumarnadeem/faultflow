#include <catch2/catch_test_macros.hpp>

#include <filesystem>

#include "db/phase1_db.hpp"

using namespace faultflow;

TEST_CASE("Phase1 DB initializes schema", "[phase1][db]") {
  const auto path = std::filesystem::temp_directory_path() /
                    "faultflow_phase1_db_test.sqlite";
  std::filesystem::remove(path);

  db::init_database(path.string());
  const db::CoverageSummary s = db::summarize(path.string());

  REQUIRE(s.total_raw_faults == 0);
  REQUIRE(s.denominator == 0);
  std::filesystem::remove(path);
}
