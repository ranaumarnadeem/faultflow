#include "db/phase1_db.hpp"

#include <SQLiteCpp/SQLiteCpp.h>

#include <stdexcept>

namespace faultflow::db {
namespace {

const char* exclusion_name(FaultExclusion exclusion) {
  switch (exclusion) {
    case FaultExclusion::NONE:
      return "none";
    case FaultExclusion::CLOCK:
      return "clock";
    case FaultExclusion::RESET:
      return "reset";
    case FaultExclusion::BLACKBOX:
      return "blackbox";
  }
  return "none";
}

const char* status_name(FaultStatus status, FaultExclusion exclusion) {
  if (exclusion != FaultExclusion::NONE) {
    return "excluded";
  }
  switch (status) {
    case FaultStatus::PENDING:
      return "undetected";
    case FaultStatus::DETECTED:
      return "detected";
    case FaultStatus::UNDETECTED:
      return "undetected";
  }
  return "undetected";
}

const char* type_name(FaultType type) {
  return type == FaultType::SA0 ? "sa0" : "sa1";
}

std::string net_name(const NormalizedGraph& ng, int yid) {
  const auto it = ng.nets.find(yid);
  if (it == ng.nets.end() || it->second.names.empty()) {
    return std::to_string(yid);
  }
  return it->second.names.front();
}

SQLite::Database open_db(const std::string& db_path) {
  SQLite::Database db(db_path, SQLite::OPEN_READWRITE | SQLite::OPEN_CREATE);
  db.exec("PRAGMA busy_timeout = 5000");
  return db;
}

bool has_column(SQLite::Database& db, const std::string& table,
                const std::string& column) {
  SQLite::Statement q(db, "PRAGMA table_info(" + table + ")");
  while (q.executeStep()) {
    if (q.getColumn(1).getString() == column) {
      return true;
    }
  }
  return false;
}

void ensure_column(SQLite::Database& db, const std::string& table,
                   const std::string& column, const std::string& spec) {
  if (!has_column(db, table, column)) {
    db.exec("ALTER TABLE " + table + " ADD COLUMN " + column + " " + spec);
  }
}

}  // namespace

void init_database(const std::string& db_path) {
  SQLite::Database db = open_db(db_path);
  db.exec(R"sql(
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS design_fingerprint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    top TEXT NOT NULL,
    netlist_hash TEXT NOT NULL,
    cell_lib_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    yosys_version TEXT NOT NULL,
    faultflow_version TEXT NOT NULL,
    collapsing INTEGER NOT NULL,
    unsupported_cells TEXT NOT NULL,
    include_clock_faults INTEGER NOT NULL,
    include_reset_faults INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL,
    vector_source TEXT,
    vector_count INTEGER NOT NULL DEFAULT 0,
    coverage REAL
);
CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    vector_index INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    inputs TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, vector_index)
);
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    net_id INTEGER NOT NULL,
    net_name TEXT NOT NULL,
    node_id INTEGER NOT NULL DEFAULT -1,
    compiled_net_index INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT '',
    fault_type TEXT NOT NULL,
    status TEXT NOT NULL,
    excluded TEXT NOT NULL DEFAULT 'none',
    exclusion TEXT NOT NULL DEFAULT 'none',
    collapsed_to INTEGER,
    collapsed_into INTEGER,
    detected_by_vector INTEGER,
    UNIQUE(compiled_net_index, fault_type)
);
CREATE TABLE IF NOT EXISTS fault_detections (
    fault_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    vector_index INTEGER NOT NULL,
    obs_net INTEGER,
    PRIMARY KEY(fault_id, run_id, vector_index)
);
CREATE TABLE IF NOT EXISTS node_coverage (
    net_id INTEGER PRIMARY KEY,
    total INTEGER NOT NULL,
    detected INTEGER NOT NULL,
    coverage REAL NOT NULL
);
)sql");
  ensure_column(db, "vectors", "inputs", "TEXT NOT NULL DEFAULT '{}'");
  ensure_column(db, "vectors", "expected", "TEXT NOT NULL DEFAULT '{}'");
  ensure_column(db, "vectors", "verified", "INTEGER NOT NULL DEFAULT 0");
  ensure_column(db, "faults", "net_name", "TEXT NOT NULL DEFAULT ''");
  ensure_column(db, "faults", "node_id", "INTEGER NOT NULL DEFAULT -1");
  ensure_column(db, "faults", "type", "TEXT NOT NULL DEFAULT ''");
  ensure_column(db, "faults", "excluded", "TEXT NOT NULL DEFAULT 'none'");
  ensure_column(db, "faults", "collapsed_to", "INTEGER");
}

int64_t start_run(const std::string& db_path, const std::string& vector_source,
                  int64_t vector_count) {
  SQLite::Database db = open_db(db_path);
  SQLite::Transaction txn(db);
  SQLite::Statement q(db,
                      "INSERT INTO runs(status, vector_source, vector_count) "
                      "VALUES ('running', ?, ?)");
  q.bind(1, vector_source);
  q.bind(2, vector_count);
  q.exec();
  const int64_t id = db.getLastInsertRowid();
  txn.commit();
  return id;
}

void write_vectors(const std::string& db_path, int64_t run_id,
                   const std::string& source,
                   const std::vector<std::string>& patterns) {
  SQLite::Database db = open_db(db_path);
  SQLite::Transaction txn(db);
  SQLite::Statement clear(db, "DELETE FROM vectors WHERE run_id = ?");
  clear.bind(1, run_id);
  clear.exec();
  SQLite::Statement q(db,
                      "INSERT INTO vectors(run_id, source, vector_index, pattern) "
                      "VALUES (?, ?, ?, ?)");
  for (size_t i = 0; i < patterns.size(); ++i) {
    q.bind(1, run_id);
    q.bind(2, source);
    q.bind(3, static_cast<int64_t>(i + 1));
    q.bind(4, patterns[i]);
    q.exec();
    q.reset();
  }
  txn.commit();
}

void write_faults(const std::string& db_path, int64_t run_id,
                  const NormalizedGraph& ng, const CompiledSimGraph& cg,
                  const std::vector<CompactFault>& faults) {
  SQLite::Database db = open_db(db_path);
  SQLite::Transaction txn(db);
  db.exec("DELETE FROM fault_detections");
  db.exec("DELETE FROM faults");
  SQLite::Statement q(
      db,
      "INSERT INTO faults(net_id, net_name, node_id, compiled_net_index, type, "
      "fault_type, status, excluded, exclusion, collapsed_to, collapsed_into, "
      "detected_by_vector) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
  SQLite::Statement det(
      db,
      "INSERT INTO fault_detections(fault_id, run_id, vector_index, obs_net) "
      "VALUES (?, ?, ?, NULL)");
  for (size_t i = 0; i < faults.size(); ++i) {
    const CompactFault& f = faults[i];
    const int yid = cg.compiled_to_yosys.at(f.net_index);
    q.bind(1, yid);
    q.bind(2, net_name(ng, yid));
    q.bind(3, static_cast<int64_t>(f.net_index));
    q.bind(4, static_cast<int64_t>(f.net_index));
    q.bind(5, type_name(f.type));
    q.bind(6, type_name(f.type));
    q.bind(7, status_name(f.status, f.exclusion));
    q.bind(8, exclusion_name(f.exclusion));
    q.bind(9, exclusion_name(f.exclusion));
    if (f.collapsed_into == UINT32_MAX) {
      q.bind(10);
      q.bind(11);
    } else {
      q.bind(10, static_cast<int64_t>(f.collapsed_into));
      q.bind(11, static_cast<int64_t>(f.collapsed_into));
    }
    if (f.detected_by_vector == 0) {
      q.bind(12);
    } else {
      q.bind(12, static_cast<int64_t>(f.detected_by_vector));
    }
    q.exec();
    const int64_t fault_id = db.getLastInsertRowid();
    q.reset();
    if (f.status == FaultStatus::DETECTED && f.detected_by_vector != 0) {
      det.bind(1, fault_id);
      det.bind(2, run_id);
      det.bind(3, static_cast<int64_t>(f.detected_by_vector));
      det.exec();
      det.reset();
    }
  }
  txn.commit();
}

CoverageSummary summarize(const std::string& db_path) {
  SQLite::Database db = open_db(db_path);
  SQLite::Statement q(db, R"sql(
SELECT
  COUNT(*) AS total_raw_faults,
  SUM(CASE WHEN exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS denominator,
  SUM(CASE WHEN status = 'detected' AND exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS detected,
  SUM(CASE WHEN status = 'undetected' AND exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS undetected,
  SUM(CASE WHEN collapsed_into IS NOT NULL THEN 1 ELSE 0 END) AS collapsed,
  SUM(CASE WHEN exclusion = 'blackbox' THEN 1 ELSE 0 END) AS excluded_blackbox,
  SUM(CASE WHEN exclusion = 'clock' THEN 1 ELSE 0 END) AS excluded_clock,
  SUM(CASE WHEN exclusion = 'reset' THEN 1 ELSE 0 END) AS excluded_reset
FROM faults
)sql");
  if (!q.executeStep()) {
    throw std::runtime_error("coverage summary query returned no row");
  }
  CoverageSummary s;
  s.total_raw_faults = q.getColumn(0).getInt64();
  s.denominator = q.getColumn(1).getInt64();
  s.detected = q.getColumn(2).getInt64();
  s.undetected = q.getColumn(3).getInt64();
  s.collapsed = q.getColumn(4).getInt64();
  s.excluded_blackbox = q.getColumn(5).getInt64();
  s.excluded_clock = q.getColumn(6).getInt64();
  s.excluded_reset = q.getColumn(7).getInt64();
  s.coverage_percent =
      s.denominator == 0 ? 0.0
                         : 100.0 * static_cast<double>(s.detected) /
                               static_cast<double>(s.denominator);
  return s;
}

void complete_run(const std::string& db_path, int64_t run_id,
                  double coverage_percent) {
  SQLite::Database db = open_db(db_path);
  SQLite::Statement q(db,
                      "UPDATE runs SET status='complete', "
                      "completed_at=CURRENT_TIMESTAMP, coverage=? WHERE id=?");
  q.bind(1, coverage_percent);
  q.bind(2, run_id);
  q.exec();
}

}  // namespace faultflow::db
