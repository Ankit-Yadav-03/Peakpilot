# Peakpilot — Industrial Decision Engine

**DERC HT Industrial Real-Time MDI Optimization**  
Delhi NCR manufacturing facilities | HT 11kV grid connections | BRPL / BYPL / TPDDL

---

## System Overview

Real-time energy decision engine for industrial HT consumers under DERC jurisdiction.

**Core functions:**
- Ingest live meter telemetry (Modbus TCP / MQTT / Simulation)
- Compute DERC HT tariff cost in real time (9-step billing formula)
- Detect 30-minute MDI peak risk with sub-window projection
- Recommend load-shed and load-shift actions with cost breakdown
- Log full decision lifecycle to SQLite (WAL-backed, crash-safe)

**Target facilities:** 200 kVA–2 MVA HT consumers, ₹10L–₹1Cr/month bills, Faridabad / Noida / Ghaziabad / Bhiwadi / Manesar

**Revenue model:** 12–15% performance contract on measured monthly savings

---

## Quick Start

```bash
# 1. Install
cd Peakpilot
pip install -r requirements.txt

# 2. Run simulation (no real meter required)
python simulate.py --minutes 60 --billing-day 2 --verbose

# 3. Run with API + Dashboard
python -m runtime.main
# Dashboard: http://localhost:8000/ui
# API docs:  http://localhost:8000/docs
# WebSocket: ws://localhost:8000/ws/live
```

---

## Repository Structure

```
Peakpilot/
├── __init__.py
├── core/
│   ├── __init__.py
│   ├── config.py          # FacilityConfig, TariffConfig, SystemConfig + loaders
│   └── models.py          # All dataclasses (MeterTick, RiskState, Recommendation, ...)
├── ingestion/
│   ├── __init__.py
│   ├── meter_stream.py    # SimulatedMeterReader, ModbusMeterReader, MQTTMeterReader
│   ├── payload_validator.py # 7 error + 4 warning physical constraint checks
│   └── register_parser.py # Modbus INT16/INT32 BE/LE, FLOAT32 — Secure Elite 440 map
├── state/
│   ├── __init__.py
│   └── state_detector.py  # 30-min MDI window, trapezoidal kVAh accumulation
├── tariff/
│   ├── __init__.py
│   ├── tariff_engine.py   # TOD window classification, voltage rebate, monthly gating
│   └── tariff_loader.py
├── cost/
│   ├── __init__.py
│   └── cost_simulator.py  # DERC HT billing Steps 1–9, counterfactual saving
├── risk/
│   ├── __init__.py
│   ├── anomaly_detector.py # Inrush suppression, load creep, stale data cap
│   └── risk_estimator.py  # SAFE/WATCH/WARNING/CRITICAL + 4 escalation rules
├── optimization/
│   ├── __init__.py
│   ├── conflict_resolver.py # Adaptive multiplier (days 1-5/6-15/16+)
│   ├── constraint_engine.py # Shed scoring, MDI impact, dependency validation
│   └── optimizer.py         # TOD shift + pre-cooling recommendations
├── decision/
│   ├── __init__.py
│   ├── action_state_manager.py # Load action state tracking
│   ├── control_policy.py    # Control policy definitions
│   ├── decision_engine.py   # 14-step per-tick orchestration
│   ├── intelligent_planner.py # Intelligent decision planning
│   └── recovery_engine.py   # Recovery logic
├── intelligence/
│   ├── __init__.py
│   ├── calibration.py       # Nightly batch: load performance + projection bias
│   └── confidence_scorer.py # 6-signal weighted confidence, familiarity stratification
├── recommendation/
│   ├── __init__.py
│   └── recommendation_engine.py # DecisionCooldown, operator-facing message format
├── learning/
│   ├── __init__.py
│   └── event_logger.py    # Two-phase WAL: sync write → async flush, crash recovery
├── runtime/
│   ├── __init__.py
│   ├── main.py            # CLI entrypoint, FastAPI + pipeline concurrent startup
│   └── pipeline.py        # Top-level orchestrator, nightly calibration scheduler
├── api/
│   ├── __init__.py
│   └── app.py             # FastAPI REST + WebSocket endpoints
├── ui/
│   ├── __init__.py
│   └── index.html         # Real-time dashboard (dark industrial, WebSocket)
├── data/
│   ├── __init__.py
│   ├── loads.json          # Facility load registry (10 loads, deps, priorities)
│   └── tariffs.json        # DERC HT parameters (FY 2021-22, frozen)
├── db/
│   ├── __init__.py
│   └── schema.sql          # WAL + 4 tables: telemetry, decision, action, outcome
├── outcome/
│   ├── __init__.py
│   ├── outcome_engine.py
│   └── outcome_worker.py
├── config.yaml
├── README.md
├── requirements.txt
├── simulate.py             # Standalone deterministic simulation runner
├── dependency_summary.txt
├── structure.txt
└── Peakpilot.db
```

---

## Pipeline (per tick — 14 steps)

```
MeterTick (Modbus/MQTT/Simulation)
  ↓ 1.  PayloadValidator          — reject errors, flag warnings
  ↓ 2.  StateDetector             — 30-min window, trapezoidal kVAh, projected MDI
  ↓ 3.  AnomalyDetector           — inrush, load creep, stale data
  ↓ 4.  TariffEngine              — TOD window, effective energy rate
  ↓ 5.  RiskEstimator             — SAFE/WATCH/WARNING/CRITICAL + escalations
  ↓ 6.  ConstraintEngine          — required reduction, load selection, dep validation
  ↓ 7.  TODOptimizer              — shift/pre-cooling opportunities
  ↓ 8.  ConflictResolver          — adaptive multiplier by billing cycle day
  ↓ 9.  ConfidenceScorer          — 6-signal weighted score, familiarity stratification
  ↓ 10. Confidence floor check    — suppress if < 0.45 (non-CRITICAL)
  ↓ 11. DecisionCooldown          — suppress same load within 10 min
  ↓ 12. RecommendationEngine      — format operator message with cost breakdown
  ↓ 13. EventLogger (WAL sync)    — write to event_wal, async flush to destination
  ↓ 14. Dashboard / API output
```

---

## DERC HT Billing Formula (11 kV, BRPL)

| Step | Component | Formula |
|------|-----------|---------|
| 1 | Base Demand Charge | `max(MDI, Contract) × ₹250` |
| 2 | MD Overdrawal Surcharge | `(MDI - Contract) × 250 × 0.30` (if MDI > Contract) |
| 3/4 | Energy Charge (with TOD) | `kVAh × 7.75 × 0.97 × TOD_multiplier` |
| 5 | Subtotal Base | Steps 1 + 2 + 3/4 |
| 6 | DRRS + Pension Trust | `Subtotal × 0.08 + Subtotal × 0.05` |
| 7 | PPAC | `Subtotal × 0.0725` (BRPL) — on Step 5 only, not nested |
| 8 | Electricity Duty | `kWh × ₹0.72` |
| 9 | Total | Steps 5+6+7+8 + Meter Rent |

**Critical billing rules:**
- TOD applies May–September only
- PPAC on Step 5 subtotal, NOT on Step 6 surcharges
- Voltage rebate on energy charges only (not demand/fixed)
- `counterfactual_mdi_kva` locked at decision time — never recomputed

---

## Risk Classification

| Ratio (proj_MDI / contract) | Level |
|-----------------------------|-------|
| < 0.85 | SAFE |
| 0.85 – 0.92 | WATCH |
| 0.92 – 1.00 | WARNING |
| ≥ 1.00 | CRITICAL |

**Escalation rules:**
- Shift changeover ±15 min: WATCH→WARNING, WARNING→CRITICAL
- Billing day ≤ 3: WATCH→WARNING
- Load creep (3 consecutive windows ≥ 3% above previous): +1 tier
- Inrush detected: suppress demand recommendations for rest of window

---

## Confidence Score (6-signal weighted)

| Signal | Weight | Default |
|--------|--------|---------|
| Projection accuracy | 0.30 | 0.60 |
| Operator compliance (stratified) | 0.25 | 0.70 |
| Load performance ratio | 0.20 | 0.70 |
| Condition familiarity | 0.15 | 0.60 |
| Data quality | 0.05 | 1.0 / 0.3 |
| Billing cycle position | 0.05 | 0.60 |

**Operator compliance is stratified by familiarity bucket** (LOW/MEDIUM/HIGH) to break correlation — both signals draw from the same history pool and unusual conditions produce low compliance. Stratification breaks this.

**Urgency thresholds:**
- ≥ 0.75 → SHOW_HIGH
- ≥ 0.55 → SHOW_MEDIUM  
- ≥ 0.35 → SHOW_LOW_WITH_FLAGS
- < 0.45 non-CRITICAL → SUPPRESS (confidence floor)
- CRITICAL → never suppress regardless of score

---

## Configuration

### data/tariffs.json
DERC HT Industrial FY 2021-22 rates (frozen). Update `ppac_*` quarterly.

### data/loads.json
Facility load registry. Key fields per load:
- `priority`: 1=never shed, 2=last resort, 3=shed first
- `shiftable`: false means never selected for demand reduction
- `max_delay_confidence`: LOW → effective delay halved at runtime
- `process_dependency`: prevents shedding if listed loads still running
- `startup_dependency`: ordering constraints

### config.yaml
```yaml
system:
  safety_margin_kva: 15.0          # Target headroom below contract
  decision_cooldown_minutes: 10    # Suppress repeat recommendation for same load
  stale_data_threshold_seconds: 90 # 3× polling_interval
  simulation_duration_minutes: 120
  api_port: 8000
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Pipeline status + tick count |
| GET | `/status` | Latest full pipeline result |
| GET | `/latest` | Latest result (503 if no data) |
| GET | `/history?limit=100` | Recent results from memory buffer |
| GET | `/decisions?limit=50` | Decision events from SQLite |
| GET | `/telemetry?limit=100` | Telemetry events from SQLite |
| GET | `/outcomes?limit=50` | Outcome events from SQLite |
| GET | `/config` | Facility + system config snapshot |
| WS | `/ws/live` | Real-time tick broadcast |
| GET | `/ui` | Dashboard HTML |

---

## Database Schema (SQLite, WAL-backed)

- **event_wal** — two-phase write buffer, crash-safe
- **telemetry_events** — per-tick meter data
- **decision_events** — recommendations with confidence, loads, savings
- **action_events** — operator responses (FOLLOWED/IGNORED/PARTIAL/MODIFIED)
- **outcome_events** — actual MDI vs projected, load_performance_ratio

---

## Stage Map

| Stage | What | Entry Requirement |
|-------|------|-------------------|
| 0 — MVP (this) | Simulation pipeline, SQLite | — |
| 1 — Real meter | Modbus/MQTT, edge deployment | Pilot facility identified |
| 2 — Infrastructure | Multi-facility, PostgreSQL, web dashboard | 3+ pilots |
| 3 — NILM | Infer loads from aggregate waveform | 6+ months submeter data |
| 4 — ML models | XGBoost forecaster, Isolation Forest | 90 days logged pairs |
| 5 — Policy learning | Optuna tuning, LightGBM, RL | 6 months, 5+ facilities |
| 6 — Autonomous control | PLC/SCADA integration | 12+ months, legal clearance |
| 7 — Platform | Cross-facility, white-label API | 20+ facilities |

---

## Known Limitations (Stage 0)

- Static load model (nameplate kW) → MDI reduction estimates ±20% initially. Converges over 30 days via `load_performance_ratio` calibration.
- Greedy load selection — not optimal for N > 8 loads. Accurate for typical factory (3–8 shiftable loads). LP solver at Stage 5.
- Linear MDI projection — over-estimates on inrush, under-estimates on gradual creep. XGBoost forecaster at Stage 4.
- Pension Trust Surcharge: 5% configured, may be 7% per stakeholder testimony. **Verify against live industrial bill before invoicing.**
- Single facility per pipeline instance. PostgreSQL + refactor at Stage 2.

---

## Safety Rules (non-negotiable)

- Priority 1 loads: **NEVER** shed under any circumstances
- Dependency violations: **NEVER** shed a load while a dependent load runs
- CRITICAL risk: **NEVER** suppress recommendation regardless of confidence
- Negative savings: raises `ValueError` — indicates counterfactual reconstruction error
- `counterfactual_mdi_kva`: locked at decision time, **never** recomputed retroactively

