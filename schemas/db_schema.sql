-- Reference SQLite schema surface for faultflow reports and scan metadata.
-- The live schema is created by faultflow/db/sqlite.py and src/core/db/sqlite_store.cpp.
PRAGMA user_version = 2;

CREATE TABLE design_fingerprint (
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
  redundancy_model_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  atpg_accepted_vectors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE vectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  vector_index INTEGER NOT NULL,
  pattern TEXT NOT NULL,
  inputs TEXT NOT NULL DEFAULT '{}',
  expected TEXT NOT NULL DEFAULT '{}',
  verified INTEGER NOT NULL DEFAULT 0,
  UNIQUE(run_id, vector_index),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE faults (
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
  redundancy_model_id TEXT,
  UNIQUE(compiled_net_index, fault_type)
);

CREATE TABLE fault_detections (
  fault_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  vector_index INTEGER NOT NULL,
  obs_net INTEGER,
  PRIMARY KEY(fault_id, run_id, vector_index),
  FOREIGN KEY(fault_id) REFERENCES faults(id),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE node_coverage (
  net_id INTEGER PRIMARY KEY,
  total INTEGER NOT NULL,
  detected INTEGER NOT NULL,
  coverage REAL NOT NULL
);
