PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS event_wal (
    wal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    written_at TEXT NOT NULL DEFAULT (datetime('now')),
    flushed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kw REAL NOT NULL,
    kva REAL NOT NULL,
    kvar REAL NOT NULL,
    kvah_cumulative REAL NOT NULL,
    pf REAL NOT NULL,
    voltage_l1 REAL NOT NULL,
    voltage_l2 REAL NOT NULL,
    voltage_l3 REAL NOT NULL,
    frequency REAL NOT NULL,
    window_start TEXT NOT NULL,
    accumulated_kvah_this_window REAL NOT NULL,
    projected_mdi_kva REAL NOT NULL,
    tod_window TEXT NOT NULL CHECK(tod_window IN ('PEAK','OFF_PEAK','NORMAL','NO_TOD')),
    source TEXT NOT NULL CHECK(source IN ('MODBUS','MQTT','SIMULATION')),
    polling_latency_ms REAL NOT NULL DEFAULT 0.0,
    data_quality TEXT NOT NULL CHECK(data_quality IN ('GOOD','INTERPOLATED','STALE'))
);

CREATE INDEX IF NOT EXISTS idx_telemetry_facility_time ON telemetry_events(facility_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_quality ON telemetry_events(data_quality);

CREATE TABLE IF NOT EXISTS decision_events (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    facility_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN ('DEMAND_RISK','TOD_OPTIMIZATION','SCHEDULED','SYSTEM')),
    risk_level TEXT NOT NULL CHECK(risk_level IN ('SAFE','WATCH','WARNING','CRITICAL')),
    projected_mdi_kva REAL NOT NULL,
    contract_demand_kva REAL NOT NULL,
    headroom_kva REAL NOT NULL,
    remaining_window_minutes REAL NOT NULL,
    tod_window TEXT NOT NULL,
    billing_cycle_day INTEGER NOT NULL,
    recommendation_type TEXT NOT NULL CHECK(recommendation_type IN ('SHED','DELAY','PRE_RUN','NO_ACTION','RESTORE')),
    loads_selected TEXT NOT NULL,
    expected_mdi_reduction_kva REAL NOT NULL,
    prevented_md_kva REAL,
    projected_saving_rupees REAL,
    economic_status TEXT,
    saving_basis TEXT,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    condition_familiarity_score REAL,
    intelligent_layer_override INTEGER NOT NULL DEFAULT 0,
    override_reason TEXT,
    conflict_resolved INTEGER NOT NULL DEFAULT 0,
    conflict_resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_facility_time ON decision_events(facility_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_risk ON decision_events(facility_id, risk_level, billing_cycle_day);

CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL REFERENCES decision_events(decision_id),
    facility_id TEXT NOT NULL,
    load_id TEXT NOT NULL,
    command_type TEXT NOT NULL CHECK(command_type IN ('SHED','RESTORE')),
    expected_state TEXT NOT NULL CHECK(expected_state IN ('OFF','ON')),
    status TEXT NOT NULL CHECK(status IN (
        'PENDING_CONFIRMATION',
        'SHED_CONFIRMED',
        'EXECUTION_NOT_CONFIRMED',
        'PENDING_RESTORE_CONFIRMATION',
        'RESTORE_CONFIRMED',
        'RESTORE_NOT_CONFIRMED'
    )),
    issued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    confirmation_latency_ms REAL,
    confirmation_source TEXT CHECK(confirmation_source IN ('EQUIPMENT','METER','EQUIPMENT_AND_METER','TIMEOUT','UNKNOWN')),
    pre_equipment_running INTEGER,
    post_equipment_running INTEGER,
    equipment_last_update TEXT,
    equipment_quality TEXT,
    pre_kva REAL,
    post_kva REAL,
    expected_delta_kva REAL NOT NULL DEFAULT 0.0,
    measured_delta_kva REAL,
    telemetry_quality TEXT,
    failure_reason TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_event_id ON execution_events(event_id);
CREATE INDEX IF NOT EXISTS idx_execution_decision_updated ON execution_events(decision_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_execution_facility_time ON execution_events(facility_id, issued_at);
CREATE INDEX IF NOT EXISTS idx_execution_status ON execution_events(facility_id, status);
CREATE INDEX IF NOT EXISTS idx_execution_load_status ON execution_events(load_id, status);

CREATE TABLE IF NOT EXISTS outcome_events (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_events(decision_id),
    facility_id TEXT NOT NULL,
    action_status TEXT NOT NULL,
    confirmation_source TEXT,
    confirmed_at TEXT,
    measured_delta_kva REAL,
    confirmation_latency_ms REAL,
    total_loads INTEGER,
    followed_loads INTEGER,
    ignored_loads INTEGER,
    compliance_pct REAL,
    projected_saving REAL,
    economic_status TEXT,
    saving_basis TEXT,
    reconciled_at TEXT DEFAULT (datetime('now', '+5 hours', '30 minutes'))
);

CREATE INDEX IF NOT EXISTS idx_outcome_facility_time
ON outcome_events(
    facility_id,
    reconciled_at
);
CREATE INDEX IF NOT EXISTS idx_outcome_decision
ON outcome_events(decision_id);

CREATE TABLE IF NOT EXISTS window_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start TEXT,
    window_end TEXT,
    actual_mdi REAL,
    accumulated_kvah REAL,
    processed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', '+5 hours', '30 minutes'))
);

CREATE INDEX IF NOT EXISTS idx_window_events_processed_id ON window_events (processed, id);

CREATE INDEX IF NOT EXISTS idx_window_events_processed_created_at ON window_events(processed, created_at);

CREATE INDEX IF NOT EXISTS idx_window_events_window_start ON window_events (window_start);

CREATE INDEX IF NOT EXISTS idx_window_events_created_at ON window_events (created_at);

CREATE TABLE IF NOT EXISTS window_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id TEXT,
    window_start TEXT,
    actual_mdi REAL,
    actual_peak REAL,
    window_kvah REAL,
    created_at TEXT DEFAULT (datetime('now', '+5 hours', '30 minutes'))
);

CREATE INDEX IF NOT EXISTS idx_window_outcomes_time ON window_outcomes (window_start);

CREATE INDEX IF NOT EXISTS idx_window_outcomes_facility_time ON window_outcomes (facility_id, window_start);

CREATE INDEX IF NOT EXISTS idx_window_outcomes_created ON window_outcomes (created_at);


CREATE TABLE IF NOT EXISTS monthly_state (
    facility_id TEXT NOT NULL,
    billing_cycle TEXT NOT NULL,
    actual_peak REAL,
    updated_at TEXT DEFAULT (datetime('now', '+5 hours', '30 minutes')),

    PRIMARY KEY (
        facility_id,
        billing_cycle
    )
);

CREATE INDEX IF NOT EXISTS idx_monthly_state_cycle ON monthly_state (facility_id, billing_cycle);
