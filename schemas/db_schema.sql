-- Reference SQLite schema surface for faultflow v3 campaign model.
PRAGMA user_version = 3;

CREATE TABLE campaigns (
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

CREATE TABLE runs (
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

CREATE TABLE vectors (
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

CREATE TABLE faults (
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

CREATE TABLE fault_detections (
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

CREATE TABLE node_coverage (
  campaign_id INTEGER NOT NULL,
  net_id INTEGER NOT NULL,
  total INTEGER NOT NULL,
  detected INTEGER NOT NULL,
  coverage REAL NOT NULL,
  PRIMARY KEY (campaign_id, net_id),
  FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE atpg_candidates (
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

CREATE TABLE candidate_rejections (
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

CREATE TABLE blocked_patterns (
  campaign_id INTEGER NOT NULL,
  fault_id INTEGER NOT NULL,
  pattern TEXT NOT NULL,
  PRIMARY KEY (campaign_id, fault_id, pattern),
  FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
  FOREIGN KEY (fault_id) REFERENCES faults(id)
);
