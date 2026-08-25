# Architecture Inventory (Derived from Runtime Code Evidence)

> Scope: Industrial Energy Optimization Platform (“Peakpilot”)
> Evidence policy: Only components and flows proven from code paths are included. Anything not provable is marked **not proven from code**.

## 1) System Inventory - Major Runtime Subsystems

### A. Runtime Entrypoint / Orchestration
- **Name**: CLI + Async Pipeline Orchestrator
- **Directory**: `runtime/`
- **Primary classes**:
  - `runtime/main.py`: `main()`, `run_api()`, `run_pipeline()`
  - `runtime/pipeline.py`: `Pipeline`
- **Responsibilities**:
  - Load configs and instantiate runtime components (meter reader, decision engine, event logger).
  - Start pipeline tick loop (async) and concurrently start FastAPI server (optional).
  - Start `OutcomeWorker` in a background thread.
  - Perform WAL crash recovery and start WAL flush worker.
- **Inputs**:
  - CLI args: `--mode`, `--billing-day`, `--no-api`, `--config`, Modbus args.
  - Config files: `config.yaml`, `data/tariffs.json`, `data/loads.json`.
  - Meter telemetry stream (async generator).
- **Outputs**:
  - Per-tick `PipelineResult` produced by `DecisionEngine.process_tick()`.
  - WebSocket broadcast payloads to `/ws/live` (via `pipeline._on_result` callback).
  - SQLite persistence through `DurableEventLogger`.
- **Dependencies (code evidence)**:
  - Imports: `core.config`, `ingestion.meter_stream`, `runtime.pipeline.Pipeline`, `api.app.create_app`.
  - Uses: `learning.event_logger.DurableEventLogger`, `outcome.outcome_worker.OutcomeWorker`.

### B. Telemetry Ingestion Layer
- **Name**: Meter Telemetry Sources
- **Directory**: `ingestion/`
- **Primary classes**:
  - `ingestion/meter_stream.py`: `BaseMeterReader` (interface), `SimulatedMeterReader`, `ModbusMeterReader`, `MQTTMeterReader`
- **Responsibilities**:
  - Provide `async for tick in reader.stream()` interface.
  - Generate/ingest meter ticks: `core.models.MeterTick` fields.
  - Emit STALE ticks on Modbus read failures.
  - MQTT supports json_flat payload parsing and deduplication.
- **Inputs**:
  - Simulation mode: facility + system config and optional `SimulatedPlantModel`.
  - Modbus mode: host/port/unit_id.
  - MQTT mode: broker host/port/topic/credentials.
- **Outputs**:
  - `MeterTick` objects yielded from `stream()`.
- **Dependencies (code evidence)**:
  - Uses `core.models.MeterTick`, `core.config` types.
  - Uses `plant_model.SimulatedPlantModel`.
  - Modbus uses `ingestion/register_parser.parse_secure_elite_440`.

### C. Payload Validation
- **Name**: Tick Payload Validator
- **Directory**: `ingestion/`
- **Primary classes**:
  - `ingestion/payload_validator.py`: `PayloadValidator`
- **Responsibilities**:
  - Validate meter ticks and return a validation object used by `DecisionEngine`.
- **Inputs**:
  - `core.models.MeterTick`.
- **Outputs**:
  - `validation` consumed by `DecisionEngine._process_tick_inner`.
- **Dependencies (code evidence)**:
  - `decision/decision_engine.py` constructs `PayloadValidator(facility_config)`.

### D. State Detection Layer
- **Name**: 30-minute Window State Detection + MDI Projection
- **Directory**: `state/`
- **Primary classes**:
  - `state/state_detector.py`: `StateDetector`
- **Responsibilities**:
  - Maintain 30-minute window tracking (accumulated kVAh and window peak kVA).
  - Close windows and schedule async callback `DecisionEngine._on_window_complete`.
  - Compute current `WindowState` and projected MDI.
- **Inputs**:
  - `process_tick(tick: MeterTick)`.
- **Outputs**:
  - `WindowState` returned per tick.
  - Callback emits `(window_state, completed_mdi)` into `DecisionEngine._window_events` queue.
- **Dependencies (code evidence)**:
  - Uses `core.models.MeterTick`, `WindowState`.
  - Called from `DecisionEngine`.

### E. Risk Analysis Layer
- **Name**: Risk Estimation + Escalations
- **Directory**: `risk/`
- **Primary classes**:
  - `risk/anomaly_detector.py`: `AnomalyDetector`
  - `risk/risk_estimator.py`: `RiskEstimator`
- **Responsibilities**:
  - `AnomalyDetector.detect()` computes:
    - inrush detection and `inrush_suppression_active`
    - load creep detection flags
    - stale data detection (`stale_data_detected`)
  - `RiskEstimator.compute()` maps projected MDI ratio to `SAFE/WATCH/WARNING/CRITICAL` with escalation rules and anomaly-driven adjustments.
  - Tracks monthly MD maximum in memory (`_monthly_md_kva`).
- **Inputs**:
  - `AnomalyDetector.detect(tick, window_start)` and `AnomalyDetector.on_window_complete(window_projected_mdi)`.
  - `RiskEstimator.compute(tick, window_state, anomaly_state, projected_mdi_kva, billing_cycle_day)`.
- **Outputs**:
  - `AnomalyState` and `RiskState`.
- **Dependencies (code evidence)**:
  - `DecisionEngine` wires both detectors.

### F. Tariff / Cost Modeling Layer
- **Name**: Tariff TOD + Voltage Rebate; DERC Cost Simulation
- **Directory**: `tariff/` and `cost/`
- **Primary classes**:
  - `tariff/tariff_engine.py`: `TariffEngine`
  - `cost/cost_simulator.py`: `CostSimulator`
- **Responsibilities**:
  - Compute `TariffState` (TOD window + effective energy rate).
  - Compute `CostState` including projected monthly bill.
- **Inputs**:
  - Tick and derived window/risk values.
- **Outputs**:
  - `TariffState`, `CostState`.
- **Dependencies (code evidence)**:
  - `DecisionEngine` sequence calls: `TariffEngine.compute` then `CostSimulator.compute`.

### G. Optimization / Planning Layer
- **Name**: Demand-load shedding planning + TOD optimization + conflict resolution
- **Directory**: `optimization/` and `decision/`
- **Primary classes**:
  - `optimization/constraint_engine.py`: `ConstraintEngine`
  - `decision/intelligent_planner.py`: `IntelligentPlanner`
  - `optimization/optimizer.py`: `TODOptimizer`
  - `optimization/conflict_resolver.py`: `ConflictResolver`
- **Responsibilities**:
  - Generate `DemandRecommendation` (planner and greedy fallback).
  - Generate `TODRecommendation`.
  - Choose winning recommendation.
- **Inputs**:
  - `required_reduction_kva`, `risk_state`, `tariff_state`, available loads.
- **Outputs**:
  - `DemandRecommendation`, `TODRecommendation`, `ConflictResolution`.

### H. Execution Management Layer
- **Name**: Execution confirmation & action-state tracking
- **Directory**: `decision/`
- **Primary classes**:
  - `decision/execution_state_manager.py`: `ExecutionStateManager`
  - `decision/action_state_manager.py`: `ActionStateManager`
  - `decision/equipment_state_manager.py`: `EquipmentStateManager` (**not fully proven in this session**)
- **Responsibilities**:
  - Create pending execution commands and reconcile into terminal statuses.
  - Persist `execution_events` via `DurableEventLogger`.
  - Track operational cooldown and restoration eligibility.

### I. Outcome Tracking
- **Name**: Outcome reconciliation background worker
- **Directory**: `outcome/`
- **Primary classes**:
  - `outcome/outcome_worker.py`: `OutcomeWorker`
  - `outcome/outcome_engine.py`: `OutcomeEngine`
- **Responsibilities**:
  - Create `outcome_events` based on terminal execution status availability.
  - Create `window_outcomes` based on processed window events.
  - Update `monthly_state` from window outcomes.

### J. Learning / Confidence Layer
- **Name**: Confidence Scoring
- **Directory**: `intelligence/` and `learning/`
- **Primary classes**:
  - `intelligence/confidence_scorer.py`: `ConfidenceScorer`
  - `learning/event_logger.py`: `DurableEventLogger`
- **Responsibilities**:
  - Produce confidence score and suppression/display action.
  - Persist event lifecycle to SQLite via `event_wal`.

### K. Recommendation Engine
- **Name**: Operator-facing recommendation formatter
- **Directory**: `recommendation/`
- **Primary classes**:
  - `recommendation/recommendation_engine.py`: `RecommendationEngine`
- **Responsibilities**:
  - Combine winning demand/TOD recommendation.
  - Apply suppression logic and CRITICAL override.
  - Create message and cost breakdown for UI/API.

### L. API Layer
- **Name**: FastAPI REST + WebSocket
- **Directory**: `api/`
- **Primary classes**:
  - `api/app.py`: `create_app`
- **Responsibilities**:
  - Provide REST endpoints and `/ws/live` WebSocket broadcast.
  - DB reads from `decision_events`, `telemetry_events`, `outcome_events`, `execution_events`.

### M. Persistence Layer
- **Name**: WAL-backed event persistence
- **Directory**: `learning/` + `db/`
- **Primary classes**:
  - `learning/event_logger.py`: `DurableEventLogger`
  - `db/schema.sql`: schema definitions
- **Responsibilities**:
  - Write event payloads to `event_wal`.
  - Async flush into destination tables.
  - Replay unflushed WAL on startup.

## 2) Database tables covered
- `event_wal`
- `telemetry_events`
- `decision_events`
- `execution_events`
- `outcome_events`
- `window_events`
- `window_outcomes`
- `monthly_state`


