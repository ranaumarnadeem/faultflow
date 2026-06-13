#include "db/sqlite_store.hpp"

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
    case FaultStatus::REDUNDANT:
      return "redundant";
  }
  return "undetected";
}

FaultStatus status_from_name(const std::string& name) {
  if (name == "detected") {
    return FaultStatus::DETECTED;
  }
  if (name == "redundant") {
    return FaultStatus::REDUNDANT;
  }
  return FaultStatus::UNDETECTED;
}

FaultExclusion exclusion_from_name(const std::string& name) {
  if (name == "clock") {
    return FaultExclusion::CLOCK;
  }
  if (name == "reset") {
    return FaultExclusion::RESET;
  }
  if (name == "blackbox") {
    return FaultExclusion::BLACKBOX;
  }
  return FaultExclusion::NONE;
}

const char* type_name(FaultType type) {
  return type == FaultType::SA0 ? "sa0" : "sa1";
}

FaultType type_from_name(const std::string& name) {
  return name == "sa1" ? FaultType::SA1 : FaultType::SA0;
}

std::string net_name(const NormalizedGraph& ng, int yid) {
  const auto it = ng.nets.find(yid);
  if (it == ng.nets.end() || it->second.names.empty()) {
    return std::to_string(yid);
  }
  return it->second.names.front();
}

std::string site_key_for_fault(const CompiledSimGraph& cg, uint32_t cidx) {
  return canonical_site_key(cg, cidx);
}

SQLite::Database open_db(const std::string& db_path) {
  SQLite::Database db(db_path, SQLite::OPEN_READWRITE | SQLite::OPEN_CREATE);
  db.exec("PRAGMA busy_timeout = 5000");
  db.exec("PRAGMA foreign_keys = ON");
  return db;
}

bool table_exists(SQLite::Database& db, const std::string& table) {
  SQLite::Statement q(db,
                      "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                      "name=?");
  q.bind(1, table);
  return q.executeStep();
}

void require_v3_schema(SQLite::Database& db) {
  const int version = db.execAndGet("PRAGMA user_version").getInt();
  if (!table_exists(db, "campaigns") || version < kSchemaUserVersion) {
    throw std::runtime_error(
        "Legacy database schema detected. Re-run with --clean.");
  }
}

void bind_fault_insert(SQLite::Statement& q, int64_t campaign_id,
                       const NormalizedGraph& ng, const CompiledSimGraph& cg,
                       const CompactFault& f) {
  const int yid = cg.compiled_to_yosys.at(f.net_index);
  const std::string site_key = site_key_for_fault(cg, f.net_index);
  q.bind(1, campaign_id);
  q.bind(2, site_key);
  q.bind(3, yid);
  q.bind(4, net_name(ng, yid));
  q.bind(5, static_cast<int64_t>(f.net_index));
  q.bind(6, static_cast<int64_t>(f.net_index));
  q.bind(7, type_name(f.type));
  q.bind(8, type_name(f.type));
  q.bind(9, status_name(f.status, f.exclusion));
  q.bind(10, exclusion_name(f.exclusion));
  q.bind(11, exclusion_name(f.exclusion));
  if (f.collapsed_into == UINT32_MAX) {
    q.bind(12);
    q.bind(13);
  } else {
    q.bind(12, static_cast<int64_t>(f.collapsed_into));
    q.bind(13, static_cast<int64_t>(f.collapsed_into));
  }
  if (f.detected_by_vector == 0) {
    q.bind(14);
  } else {
    q.bind(14, static_cast<int64_t>(f.detected_by_vector));
  }
  q.bind(15);
}

}  // namespace

void init_database(const std::string& db_path) {
  SQLite::Database db = open_db(db_path);
  if (table_exists(db, "faults") &&
      (!table_exists(db, "campaigns") ||
       db.execAndGet("PRAGMA user_version").getInt() < kSchemaUserVersion)) {
    throw std::runtime_error(
        "Legacy database schema detected. Re-run with --clean.");
  }
  db.exec(R"sql(
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_type TEXT NOT NULL CHECK (campaign_type IN ('comb', 'scan')),
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
    redundancy_model_id TEXT NOT NULL DEFAULT '',
    manifest_hash TEXT NOT NULL DEFAULT '',
    atpg_view_schema_ver TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL,
    vector_source TEXT,
    vector_count INTEGER NOT NULL DEFAULT 0,
    initial_ff_state TEXT NOT NULL DEFAULT 'all_zero',
    atpg_generation_seconds REAL NOT NULL DEFAULT 0.0,
    fault_simulation_seconds REAL NOT NULL DEFAULT 0.0,
    total_sim_seconds REAL NOT NULL DEFAULT 0.0,
    coverage REAL,
    atpg_terminal_reason TEXT,
    atpg_rounds INTEGER NOT NULL DEFAULT 0,
    atpg_sat INTEGER NOT NULL DEFAULT 0,
    atpg_unsat INTEGER NOT NULL DEFAULT 0,
    atpg_timeout INTEGER NOT NULL DEFAULT 0,
    atpg_unknown INTEGER NOT NULL DEFAULT 0,
    atpg_rejected_candidates INTEGER NOT NULL DEFAULT 0,
    atpg_generated_vectors INTEGER NOT NULL DEFAULT 0,
    atpg_accepted_vectors INTEGER NOT NULL DEFAULT 0,
    protocol_no_progress_rounds INTEGER NOT NULL DEFAULT 0,
    candidates_aborted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    vector_index INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    inputs TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    UNIQUE (campaign_id, id),
    UNIQUE (campaign_id, run_id, vector_index),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    fault_site_key TEXT NOT NULL,
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
    redundancy_model_id TEXT,
    protocol_unresolved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (campaign_id, fault_site_key, fault_type),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS fault_detections (
    fault_id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    vector_index INTEGER NOT NULL,
    obs_net INTEGER,
    PRIMARY KEY (fault_id, run_id, vector_index),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (fault_id) REFERENCES faults(id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS node_coverage (
    campaign_id INTEGER NOT NULL,
    net_id INTEGER NOT NULL,
    total INTEGER NOT NULL,
    detected INTEGER NOT NULL,
    coverage REAL NOT NULL,
    PRIMARY KEY (campaign_id, net_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS atpg_candidates (
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    sat_target_fault_id INTEGER,
    accepted_vector_id INTEGER,
    PRIMARY KEY (campaign_id, run_id, candidate_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (campaign_id, accepted_vector_id)
        REFERENCES vectors(campaign_id, id)
);
CREATE TABLE IF NOT EXISTS candidate_rejections (
    campaign_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    PRIMARY KEY (campaign_id, run_id, candidate_id, fault_id),
    FOREIGN KEY (campaign_id, run_id, candidate_id)
        REFERENCES atpg_candidates(campaign_id, run_id, candidate_id),
    FOREIGN KEY (fault_id) REFERENCES faults(id)
);
CREATE TABLE IF NOT EXISTS blocked_patterns (
    campaign_id INTEGER NOT NULL,
    fault_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    PRIMARY KEY (campaign_id, fault_id, pattern),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (fault_id) REFERENCES faults(id)
);
)sql");
  db.exec("PRAGMA user_version = " + std::to_string(kSchemaUserVersion));
}

int64_t fault_count(const std::string& db_path, int64_t campaign_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(db,
                      "SELECT COUNT(*) FROM faults WHERE campaign_id = ?");
  q.bind(1, campaign_id);
  if (!q.executeStep()) {
    return 0;
  }
  return q.getColumn(0).getInt64();
}

int64_t start_run(const std::string& db_path, int64_t campaign_id,
                  const std::string& vector_source, int64_t vector_count,
                  const std::string& initial_ff_state) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement q(
      db, "INSERT INTO runs(campaign_id, status, vector_source, vector_count, "
          "initial_ff_state) VALUES (?, 'running', ?, ?, ?)");
  q.bind(1, campaign_id);
  q.bind(2, vector_source);
  q.bind(3, vector_count);
  q.bind(4, initial_ff_state);
  q.exec();
  const int64_t id = db.getLastInsertRowid();
  txn.commit();
  return id;
}

void write_vectors(const std::string& db_path, int64_t campaign_id,
                   int64_t run_id, const std::string& source,
                   const std::vector<std::string>& patterns) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement clear(db,
                          "DELETE FROM vectors WHERE run_id = ? AND campaign_id "
                          "= ?");
  clear.bind(1, run_id);
  clear.bind(2, campaign_id);
  clear.exec();
  SQLite::Statement q(
      db, "INSERT INTO vectors(campaign_id, run_id, source, vector_index, "
          "pattern) VALUES (?, ?, ?, ?, ?)");
  for (size_t i = 0; i < patterns.size(); ++i) {
    q.bind(1, campaign_id);
    q.bind(2, run_id);
    q.bind(3, source);
    q.bind(4, static_cast<int64_t>(i + 1));
    q.bind(5, patterns[i]);
    q.exec();
    q.reset();
  }
  txn.commit();
}

void append_vectors(const std::string& db_path, int64_t campaign_id,
                    int64_t run_id, const std::string& source,
                    const std::vector<std::string>& patterns,
                    int64_t start_index) {
  if (patterns.empty()) {
    return;
  }
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement q(
      db, "INSERT INTO vectors(campaign_id, run_id, source, vector_index, "
          "pattern) VALUES (?, ?, ?, ?, ?)");
  for (size_t i = 0; i < patterns.size(); ++i) {
    q.bind(1, campaign_id);
    q.bind(2, run_id);
    q.bind(3, source);
    q.bind(4, start_index + static_cast<int64_t>(i));
    q.bind(5, patterns[i]);
    q.exec();
    q.reset();
  }
  txn.commit();
}

void insert_faults_if_empty(const std::string& db_path, int64_t campaign_id,
                            const NormalizedGraph& ng,
                            const CompiledSimGraph& cg,
                            const std::vector<CompactFault>& faults) {
  if (fault_count(db_path, campaign_id) > 0) {
    return;
  }
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement q(
      db,
      "INSERT INTO faults(campaign_id, fault_site_key, net_id, net_name, "
      "node_id, compiled_net_index, type, fault_type, status, excluded, "
      "exclusion, collapsed_to, collapsed_into, detected_by_vector, "
      "redundancy_model_id) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
  for (const CompactFault& f : faults) {
    bind_fault_insert(q, campaign_id, ng, cg, f);
    q.exec();
    q.reset();
  }
  txn.commit();
}

void write_faults(const std::string& db_path, int64_t campaign_id,
                  int64_t run_id, const NormalizedGraph& ng,
                  const CompiledSimGraph& cg,
                  const std::vector<CompactFault>& faults) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement clear_det(
      db, "DELETE FROM fault_detections WHERE campaign_id = ?");
  clear_det.bind(1, campaign_id);
  clear_det.exec();
  SQLite::Statement clear_faults(db,
                                 "DELETE FROM faults WHERE campaign_id = ?");
  clear_faults.bind(1, campaign_id);
  clear_faults.exec();
  SQLite::Statement q(
      db,
      "INSERT INTO faults(campaign_id, fault_site_key, net_id, net_name, "
      "node_id, compiled_net_index, type, fault_type, status, excluded, "
      "exclusion, collapsed_to, collapsed_into, detected_by_vector, "
      "redundancy_model_id) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
  SQLite::Statement det(
      db,
      "INSERT INTO fault_detections(fault_id, campaign_id, run_id, "
      "vector_index, obs_net) VALUES (?, ?, ?, ?, NULL)");
  for (const CompactFault& f : faults) {
    bind_fault_insert(q, campaign_id, ng, cg, f);
    q.exec();
    const int64_t fault_id = db.getLastInsertRowid();
    q.reset();
    if (f.status == FaultStatus::DETECTED && f.detected_by_vector != 0) {
      det.bind(1, fault_id);
      det.bind(2, campaign_id);
      det.bind(3, run_id);
      det.bind(4, static_cast<int64_t>(f.detected_by_vector));
      det.exec();
      det.reset();
    }
  }
  txn.commit();
}

FaultRecord load_fault(const std::string& db_path, int64_t fault_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(
      db, "SELECT id, campaign_id, compiled_net_index, fault_type, status, "
          "exclusion, collapsed_into, protocol_unresolved FROM faults WHERE "
          "id = ?");
  q.bind(1, fault_id);
  if (!q.executeStep()) {
    throw std::runtime_error("fault not found: " + std::to_string(fault_id));
  }
  FaultRecord rec;
  rec.id = q.getColumn(0).getInt64();
  rec.campaign_id = q.getColumn(1).getInt64();
  rec.compiled_net_index = static_cast<uint32_t>(q.getColumn(2).getInt64());
  rec.type = type_from_name(q.getColumn(3).getString());
  rec.status = status_from_name(q.getColumn(4).getString());
  rec.exclusion = exclusion_from_name(q.getColumn(5).getString());
  if (q.getColumn(6).isNull()) {
    rec.collapsed_into = UINT32_MAX;
  } else {
    rec.collapsed_into = static_cast<uint32_t>(q.getColumn(6).getInt64());
  }
  rec.protocol_unresolved = q.getColumn(7).getInt() != 0;
  return rec;
}

void mark_fault_detected(const std::string& db_path, int64_t campaign_id,
                         int64_t run_id, int64_t fault_id,
                         int64_t vector_index) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Transaction txn(db);
  SQLite::Statement q(db,
                      "UPDATE faults SET status='detected', detected_by_vector=?, "
                      "protocol_unresolved=0 WHERE id=?");
  q.bind(1, vector_index);
  q.bind(2, fault_id);
  q.exec();
  SQLite::Statement det(
      db,
      "INSERT OR IGNORE INTO fault_detections(fault_id, campaign_id, run_id, "
      "vector_index, obs_net) VALUES (?, ?, ?, ?, NULL)");
  det.bind(1, fault_id);
  det.bind(2, campaign_id);
  det.bind(3, run_id);
  det.bind(4, vector_index);
  det.exec();
  txn.commit();
}

void mark_fault_redundant(const std::string& db_path, int64_t fault_id,
                          const std::string& redundancy_model_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(
      db, "UPDATE faults SET status='redundant', redundancy_model_id=?, "
          "detected_by_vector=NULL WHERE id=?");
  q.bind(1, redundancy_model_id);
  q.bind(2, fault_id);
  q.exec();
}

void mark_fault_protocol_unresolved(const std::string& db_path,
                                    int64_t fault_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(
      db, "UPDATE faults SET protocol_unresolved=1 WHERE id=? AND status != "
          "'detected'");
  q.bind(1, fault_id);
  q.exec();
}

void invalidate_stale_redundant(const std::string& db_path,
                                int64_t campaign_id,
                                const std::string& redundancy_model_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(
      db,
      "UPDATE faults SET status='undetected', redundancy_model_id=NULL "
      "WHERE campaign_id = ? AND status='redundant' AND "
      "(redundancy_model_id IS NULL OR redundancy_model_id != ?)");
  q.bind(1, campaign_id);
  q.bind(2, redundancy_model_id);
  q.exec();
}

void update_run_vector_count(const std::string& db_path, int64_t run_id,
                             int64_t vector_count) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(db, "UPDATE runs SET vector_count=? WHERE id=?");
  q.bind(1, vector_count);
  q.bind(2, run_id);
  q.exec();
}

CoverageSummary summarize(const std::string& db_path, int64_t campaign_id) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(db, R"sql(
SELECT
  COUNT(*) AS total_raw_faults,
  SUM(CASE WHEN exclusion = 'none' AND collapsed_into IS NULL AND status != 'redundant' THEN 1 ELSE 0 END) AS denominator,
  SUM(CASE WHEN status = 'detected' AND exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS detected,
  SUM(CASE WHEN status = 'undetected' AND exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS undetected,
  SUM(CASE WHEN status = 'redundant' AND exclusion = 'none' AND collapsed_into IS NULL THEN 1 ELSE 0 END) AS redundant,
  SUM(CASE WHEN collapsed_into IS NOT NULL THEN 1 ELSE 0 END) AS collapsed,
  SUM(CASE WHEN exclusion = 'blackbox' THEN 1 ELSE 0 END) AS excluded_blackbox,
  SUM(CASE WHEN exclusion = 'clock' THEN 1 ELSE 0 END) AS excluded_clock,
  SUM(CASE WHEN exclusion = 'reset' THEN 1 ELSE 0 END) AS excluded_reset
FROM faults
WHERE campaign_id = ?
)sql");
  q.bind(1, campaign_id);
  if (!q.executeStep()) {
    throw std::runtime_error("coverage summary query returned no row");
  }
  CoverageSummary s;
  s.total_raw_faults = q.getColumn(0).getInt64();
  s.denominator = q.getColumn(1).getInt64();
  s.detected = q.getColumn(2).getInt64();
  s.undetected = q.getColumn(3).getInt64();
  s.redundant = q.getColumn(4).getInt64();
  s.collapsed = q.getColumn(5).getInt64();
  s.excluded_blackbox = q.getColumn(6).getInt64();
  s.excluded_clock = q.getColumn(7).getInt64();
  s.excluded_reset = q.getColumn(8).getInt64();
  s.coverage_percent =
      s.denominator == 0 ? 0.0
                         : 100.0 * static_cast<double>(s.detected) /
                               static_cast<double>(s.denominator);
  return s;
}

void complete_run(const std::string& db_path, int64_t run_id,
                  double coverage_percent) {
  AtpgRunStats stats;
  complete_run_with_atpg(db_path, run_id, coverage_percent, stats);
}

void complete_run_with_atpg(const std::string& db_path, int64_t run_id,
                            double coverage_percent, const AtpgRunStats& stats) {
  SQLite::Database db = open_db(db_path);
  require_v3_schema(db);
  SQLite::Statement q(
      db,
      "UPDATE runs SET status='complete', completed_at=CURRENT_TIMESTAMP, "
      "coverage=?, atpg_terminal_reason=?, atpg_rounds=?, atpg_sat=?, "
      "atpg_unsat=?, atpg_timeout=?, atpg_unknown=?, atpg_rejected_candidates=?, "
      "atpg_generated_vectors=?, atpg_accepted_vectors=? WHERE id=?");
  q.bind(1, coverage_percent);
  if (stats.terminal_reason.empty()) {
    q.bind(2);
  } else {
    q.bind(2, stats.terminal_reason);
  }
  q.bind(3, stats.rounds);
  q.bind(4, stats.sat);
  q.bind(5, stats.unsat);
  q.bind(6, stats.timeout);
  q.bind(7, stats.unknown);
  q.bind(8, stats.rejected_candidates);
  q.bind(9, stats.generated_vectors);
  q.bind(10, stats.accepted_vectors);
  q.bind(11, run_id);
  q.exec();
}

}  // namespace faultflow::db
