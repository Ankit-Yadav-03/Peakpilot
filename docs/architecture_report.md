# Architecture Report - Industrial Energy Optimization Platform (Validation-Ready)

> Generation rule: this document is derived strictly from runtime code evidence in the repository. Any statement not directly supported is omitted or marked **not proven from code**.

## Executive Summary (What the system does)
The platform performs real-time industrial energy decisioning for a single facility instance. It continuously ingests meter telemetry (Simulation, Modbus TCP, or MQTT), computes a 30-minute MDI (Maximum Demand Index) window state with sub-window projection, estimates risk levels, computes tariff-based cost projections (including TOD and voltage rebate logic), and generates operator-facing recommendations for demand protection actions (shed) and operational tariff optimization advisories (TOD pre-run / shift). It then tracks execution confirmation using both equipment telemetry (equipment state manager) and meter confirmation thresholds, reconciles outcomes in a background worker, and records the full lifecycle into an SQLite database using a WAL-backed two-phase persistence design.

## Decision-making (How decisions are made)
### 1) Telemetry tick entry
The runtime entrypoint is `runtime/main.py`, which builds an async `Pipeline` (`runtime/pipeline.py`) using an ingestion “meter reader” (`ingestion/meter_stream.py`). Each reader provides an async stream of `core.models.MeterTick` objects.

### 2) Per-tick processing orchestration
`runtime/pipeline.py` consumes ticks and calls `decision/decision_engine.py`:
- `DecisionEngine.process_tick(tick)` (guarded by an async lock)

Within `DecisionEngine._process_tick_inner`, the tick flow is:
1. **Validation**: `ingestion/payload_validator.PayloadValidator.validate(tick)`
2. **State detection**: `state/state_detector.StateDetector.process_tick(tick)` returns `WindowState` and maintains a 30-minute window.
3. **Window closure callback**: when the 30-minute window closes, `StateDetector` schedules `on_window_complete(window_state, mdi)` into an internal queue (`DecisionEngine._window_events`). The decision engine drains this queue before current tick risk computation.
4. **Anomaly detection**: `risk/anomaly_detector.AnomalyDetector.detect(tick, window_start)`
5. **Tariff modeling**: `tariff/tariff_engine.TariffEngine.compute(tick)`
6. **Risk estimation**: `risk/risk_estimator.RiskEstimator.compute(...)` maps projected MDI ratio to `SAFE/WATCH/WARNING/CRITICAL` with escalation rules and anomaly-driven adjustments.
7. **Cost simulation**: `cost/cost_simulator.CostSimulator.compute(...)` (sequence proven; full internal formulae not detailed here because `cost_simulator.py` was not read in this analysis session)
8. **Execution reconciliation**: `decision/execution_state_manager.ExecutionStateManager.reconcile(tick)` updates execution record statuses based on equipment freshness/confirmation and meter delta thresholds.
9. **Control policy gating**: `decision/control_policy.ControlPolicy.evaluate(risk_state)` determines whether to plan actions and required reduction.
10. **Optimization and planning**:
   - Demand reduction uses:
     - `optimization/constraint_engine.ConstraintEngine` and `decision/intelligent_planner.IntelligentPlanner` to generate `DemandRecommendation`.
     - A greedy fallback to `ConstraintEngine.select_loads_to_shed(...)` may be used when planning fails under CRITICAL risk.
   - TOD advisories use `optimization/optimizer.TODOptimizer.compute(...)`.
11. **Conflict resolution**: `optimization/conflict_resolver.ConflictResolver.resolve(...)` selects the winning recommendation based on risk level and billing-cycle position.
12. **Recommendation formatting + suppression**: `recommendation/recommendation_engine.RecommendationEngine.build(...)`:
   - applies suppression rules derived from `intelligence/confidence_scorer.ConfidenceScorer.compute(...)`
   - contains a CRITICAL override that ensures recommendations are **not suppressed**.
13. **Scheduling execution**:
   - if the final recommendation type is `SHED` or `RESTORE`, `ExecutionStateManager.create_pending_for_recommendation(...)` creates pending execution records.

### 3) Learning/confidence shaping the recommendation
`intelligence/confidence_scorer.ConfidenceScorer` computes confidence using six signal categories (projection accuracy, execution confirmation, load performance, condition familiarity, data quality, and billing cycle position). It produces:
- `display_action` for operator UI messaging
- `suppressed` + `suppression_reason` that drive whether a recommendation is hidden

A CRITICAL risk path explicitly overrides suppression in `RecommendationEngine`.

## Execution verification (How execution is verified)
Execution verification is handled within `decision/execution_state_manager.ExecutionStateManager` (reconciliation is driven by the pipeline tick loop):
- When a recommendation is issued, `create_pending_for_recommendation(...)` creates `ExecutionRecord`s with:
  - expected meter delta (`expected_delta_kva`)
  - equipment pre-state (via `equipment_manager.get_state(load_id)`)
  - expected final state (`OFF` for SHED, `ON` for RESTORE)
  - status `PENDING_CONFIRMATION` / `PENDING_RESTORE_CONFIRMATION`
- On each tick, `reconcile(tick)` updates each record until a terminal status is reached using:
  - equipment confirmation based on equipment state freshness and expected state match
  - meter confirmation based on measured delta threshold and expected delta
  - timeouts generating terminal statuses (`EXECUTION_NOT_CONFIRMED` or `RESTORE_NOT_CONFIRMED`)
- After updates, record changes are persisted via `DurableEventLogger.log_execution_event(...)`.


## Outcome reconciliation and outcome recording
A background `OutcomeWorker` (`outcome/outcome_worker.py`) runs in a daemon thread started from `runtime/main.py`. It performs two reconciliation loops driven by DB polling:

1. **Decision outcomes**
   - It selects `decision_events` that do not yet appear in `outcome_events`.
   - It queries `execution_events` for each decision.
   - When execution statuses reach terminal states (confirmed or not confirmed), it creates a corresponding `outcome_events` record via `DurableEventLogger.log_outcome_event(...)`.

2. **Window outcomes**
   - It selects `window_events` where `processed=0`.
   - For each, it runs `OutcomeEngine.compute(conn, window_event)` which updates `monthly_state` and returns computed window outcome fields.
   - It persists `window_outcomes` and then marks the window event as processed.

## How persistence works (SQLite WAL design)
Persistence is centralized in `learning/event_logger.DurableEventLogger`.
- **Two-phase WAL protocol**:
  - Phase 1 (sync): writes payloads into `event_wal` with `flushed=0`.
  - Phase 2 (async flush worker): flushes unflushed WAL records into destination tables and marks `flushed=1`.
- **Crash recovery**:
  - `runtime/pipeline.Pipeline.run()` calls `DurableEventLogger.replay_wal()` before starting the flush worker.

## API and operational visibility
FastAPI (`api/app.py`) exposes:
- REST endpoints for `/health`, `/status`, `/latest`, `/history`, and DB-backed endpoints like `/decisions`, `/telemetry`, `/outcomes`, `/executions`.
- WebSocket endpoint `/ws/live` broadcasts the latest `PipelineResult` as live JSON payloads.

WebSocket payloads are produced by a pipeline callback wired in `api/app.py` (`pipeline._on_result = _on_result_callback`).

## Runtime entrypoints and lifecycle
### Startup sequence (proved from code)
1. `python -m runtime.main` executes `runtime/main.py:main()`.
2. Loads config: `core.config.load_system_config`, `load_tariff_config`, `load_facility_config`.
3. Instantiates meter reader based on `--mode`.
4. Instantiates `Pipeline` (which instantiates `DurableEventLogger`, `DecisionEngine`, `StateDetector`, etc.).
5. Creates `OutcomeEngine` and `OutcomeWorker` and starts `OutcomeWorker` in a daemon thread.
6. If API enabled, `api/app.py:create_app(...)` creates FastAPI app.
7. Runs `asyncio.gather(run_pipeline(pipeline), run_api(app))`.

### Shutdown sequence (partial evidence)
- `Pipeline.run()` handles cancellation: cancels equipment stream task, stops WAL flush worker.
- `OutcomeWorker` is a daemon thread and runs infinite loop; explicit shutdown behavior is **not proven from code**.

## Evidence map - Key artifacts
- Runtime orchestrator: `runtime/main.py`, `runtime/pipeline.py`
- Telemetry: `ingestion/meter_stream.py`
- State & risk: `state/state_detector.py`, `risk/anomaly_detector.py`, `risk/risk_estimator.py`
- Tariff & cost: `tariff/tariff_engine.py`, `cost/cost_simulator.py`
- Optimization & arbitration: `optimization/constraint_engine.py`, `decision/intelligent_planner.py`, `optimization/optimizer.py`, `optimization/conflict_resolver.py`
- Recommendation: `recommendation/recommendation_engine.py`
- Execution tracking: `decision/execution_state_manager.py`, `decision/action_state_manager.py`
- Outcome reconciliation: `outcome/outcome_worker.py`, `outcome/outcome_engine.py`
- Persistence: `learning/event_logger.py`, `db/schema.sql`
- API exposure: `api/app.py`


