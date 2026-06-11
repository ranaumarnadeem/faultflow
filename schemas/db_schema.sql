-- Reference SQLite schema surface for faultflow reports and scan metadata.
-- The live schema is created by faultflow/db/sqlite.py and src/core/db.
PRAGMA user_version = 1;

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
  include_reset_faults INTEGER NOT NULL
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  vector_source TEXT NOT NULL,
  vector_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  coverage_percent REAL
);

CREATE TABLE vectors (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  vector_index INTEGER NOT NULL,
  inputs TEXT NOT NULL,
  expected TEXT,
  verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE faults (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  net_id INTEGER NOT NULL,
  fault_type TEXT NOT NULL,
  status TEXT NOT NULL,
  exclusion TEXT,
  collapsed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE fault_detections (
  fault_id INTEGER NOT NULL,
  vector_id INTEGER NOT NULL,
  PRIMARY KEY (fault_id, vector_id)
);
