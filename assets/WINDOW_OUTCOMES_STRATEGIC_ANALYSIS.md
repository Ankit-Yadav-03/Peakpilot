# window_outcomes Table: Strategic Analysis for Future Model Training

**Query**: Is `window_outcomes` important and uniquely necessary for future ML model training?

**Answer**: ✓ **YES - CRITICAL for Stages 4-5 ML**, but currently unfilled due to missing `OutcomeEngine.compute()` pipeline.

---

## Part 1: What window_outcomes Contains (Schema)

```sql
window_outcomes (
    id PRIMARY KEY,
    facility_id TEXT,           ← Facility grouping
    window_start TEXT,          ← Time series key
    
    actual_mdi REAL,            ← GROUND TRUTH (measured)
    baseline_mdi REAL,          ← COUNTERFACTUAL (what would happen without intervention)
    
    actual_peak REAL,           ← Monthly cumulative peak (actual with interventions)
    baseline_peak REAL,         ← Monthly cumulative peak (counterfactual without interventions)
    
    demand_saving REAL,         ← REALIZED SAVING (rupees) - actual economic outcome
    energy_saving REAL,         ← NOT IMPLEMENTED (0.0)
    total_saving REAL,          ← Alias for demand_saving
    
    actual_bill REAL,           ← NOT IMPLEMENTED
    baseline_bill REAL,         ← NOT IMPLEMENTED
    
    window_kvah REAL,           ← Energy consumption window total
    window_kwh REAL,            ← NOT IMPLEMENTED (0.0)
    
    created_at TIMESTAMP
)
```

**Indexes**:
- `(window_start)` - Time series queries
- `(facility_id, window_start)` - **Unique grouping by facility + window** ✓
- `(created_at)` - Insertion order tracking

---

## Part 2: Why This Data Structure Is Unique & Important

### 2.1: Causal Attribution (Ground Truth for ML)

**The Core Problem window_outcomes Solves**:
```
Decision Engine says:
  "If you shed Compressor A (expected reduction: 50 kVA),
   you'll save ₹2,500 this window"
   
What actually happened:
  Actual MDI was 180 kVA (baseline would be 230 kVA)
  → Realized saving: ₹2,100 (NOT ₹2,500)
  → Projection error: -16%
  
window_outcomes captures this ground truth ↓

{
  window_start: "2026-01-15T14:30:00",
  actual_mdi: 180.0,
  baseline_mdi: 230.0,
  demand_saving: 2100.0,
  ...
}
```

This is the **only table that contains counterfactual pairs**:
- `actual_mdi` = what happened WITH interventions
- `baseline_mdi` = what would have happened WITHOUT interventions (estimated)

**Why unique**: 
- `decision_events` has PROJECTED savings (forecast)
- `execution_events` has MEASURED actions (commands executed)
- `outcome_events` has DECISION reconciliation (did operator follow?)
- **Only `window_outcomes` has ECONOMIC ground truth (actual vs counterfactual)**

---

### 2.2: Aggregation Level Matters

```
Raw data granularity:
  telemetry_events     → per-tick (every 15 sec)     → 240 rows/day
  decision_events      → per decision (~10-20/day)   → 100 rows/day
  execution_events     → per load action (~50/day)   → 50 rows/day
  
Aggregated (window level):
  window_events        → per 30-min window           → 48 rows/day
  window_outcomes      → per 30-min window           → 48 rows/day
                          ↑ GROUPED BY: (facility_id, window_start)
```

**Why window-level aggregation is needed**:
1. **MDI billing is per-window** - 30-min demand window is the cost unit
2. **Causal inference** - need to isolate impact of interventions within each window
3. **Feature stability** - window-level metrics are less noisy than per-tick
4. **ML training** - 1 sample = 1 window outcome (X: conditions, y: realized_saving)

---

## Part 3: Future ML Models That Depend on window_outcomes

### Stage 4: Projection Accuracy Improvement (90 days data)

**Goal**: Replace linear MDI projection with XGBoost forecaster

**Training Data Required**:
```python
# From window_outcomes:
training_set = [
    {
        "window_start": "2026-01-15T14:30:00",
        "facility_id": "FAC-001",
        
        # FEATURES (from decision_events + telemetry):
        "tod_window": "PEAK",
        "billing_cycle_day": 15,
        "hour_of_day": 14,
        "day_of_week": "MON",
        "ambient_temp": 32,
        "loads_running": ["Compressor_A", "Chiller_B"],
        
        # TARGET (from window_outcomes):
        "actual_mdi": 195.3,  # Ground truth
        "baseline_mdi": 225.7, # Model prediction to improve
        "projection_error": -8.1,  # (predicted - actual) / actual * 100
    },
    ...  # Repeat for 90+ days × 48 windows/day = 4,320+ samples
]
```

**Model Objective**:
- Input: baseline_mdi (current linear projection)
- Learn: Correction factor based on conditions
- Output: Better forecasts → Better decision confidence → Better recommendations

**Data requirement**: 
- ✓ `window_outcomes.baseline_mdi` (current projection)
- ✓ `window_outcomes.actual_mdi` (ground truth to compare)
- ✓ `window_events.window_kvah` (context for conditions)
- ✓ `decision_events` features (TOD, risk_level, loads_selected)
- ✓ `telemetry_events` (hour_of_day, conditions)

---

### Stage 5: Policy Learning & Reinforcement Learning (6+ months, 5+ facilities)

**Goal**: Optuna tuning + LightGBM + RL for optimal load dispatch policy

**Training Dataset**:
```python
# From combined tables:
rl_dataset = [
    {
        # CONTEXT (state):
        "facility_id": "FAC-001",
        "window_start": "2026-01-15T14:30:00",
        "mdi_current": 190,
        "contract_demand": 250,
        "remaining_minutes": 25,
        "loads_available": ["Pump_A", "Pump_B", "AC_Unit", "Heater"],
        "loads_running": ["AC_Unit"],
        
        # ACTION (policy decision):
        "action": "SHED_Pump_A_and_B",
        "expected_mdi_reduction": 45.0,
        
        # OUTCOME (from window_outcomes):
        "realized_mdi": 165.0,
        "realized_saving": 3200.0,  # Rupees
        "peak_exceeded": False,
        
        # REWARD (RL signal):
        "reward": 3200.0 - 500.0 * <discomfort_penalty>,
        
        # FEATURES FOR LightGBM:
        "projection_error": -8.5,
        "execution_confirmation_rate": 0.92,
        "load_performance_ratio": 0.95,
        "billing_cycle_position": 15,
        "condition_familiarity": 0.7,
    },
    ...  # ~8,640 windows/month × 6+ months × 5+ facilities
]
```

**Models Needed**:
1. **Policy network** (RL): state → best action (which loads to shed)
2. **Value network** (RL): state → expected return (profit)
3. **LightGBM classifier**: Predict load performance ratios
4. **Optuna objective**: Maximize (realized_saving - constraints_violated)

**Data required from window_outcomes**:
- ✓ `actual_mdi`, `baseline_mdi` - ground truth outcomes
- ✓ `demand_saving` - reward signal
- ✓ `facility_id`, `window_start` - grouping for multi-facility learning
- ✗ `energy_saving`, `actual_bill` - currently not implemented (would improve signals)

---

## Part 4: Field-by-Field Strategic Value

| Field | Current Status | Used By | Future Stage | ML Value |
|-------|--------|---------|----------------|----------|
| **facility_id** | ✓ Defined | Grouping | All | HIGH - Multi-facility aggregation |
| **window_start** | ✓ Defined | Time series key | All | HIGH - Time series ordering, billing cycle tracking |
| **actual_mdi** | ✓ In schema | Ground truth | 4, 5 | **CRITICAL** - Target variable for forecasting |
| **baseline_mdi** | ✓ In schema | Ground truth | 4, 5 | **CRITICAL** - Current model performance baseline |
| **actual_peak** | ✓ Defined | Monthly peak tracking | 4, 5 | MEDIUM - Cumulative risk tracking |
| **baseline_peak** | ✓ Defined | Counterfactual tracking | 4, 5 | MEDIUM - What-if analysis |
| **demand_saving** | ✓ In schema | Reward signal | 5 (RL) | **CRITICAL** - RL reward; economic outcome |
| **energy_saving** | ✗ NOT IMPLEMENTED | Future energy tariffs | Future | LOW (for now) |
| **total_saving** | ✓ Defined | Economic outcome | 4, 5 | HIGH - Primary objective function |
| **actual_bill** | ✗ NOT IMPLEMENTED | Cost tracking | 5+ | MEDIUM - Could improve financial signals |
| **baseline_bill** | ✗ NOT IMPLEMENTED | Counterfactual costs | 5+ | MEDIUM - Impact analysis |
| **window_kvah** | ✓ Defined | Energy context | 4, 5 | MEDIUM - Feature for load projection |
| **window_kwh** | ✗ NOT IMPLEMENTED | TOD tariff impact | 5+ | LOW (currently zero) |

---

## Part 5: Data Grouping: Unique & Necessary?

### Question: Why not just use existing tables?

**Hypothesis**: Could we rebuild window_outcomes from decision_events + execution_events?

**Answer**: ❌ **NO - That would lose causal information**

```python
# WRONG APPROACH:
decision = decision_events.get(decision_id)
execution = execution_events.filter(decision_id == decision_id)
calculated_outcome = {
    "projected_saving": decision.projected_saving_rupees,
    "actual_reduction": sum(execution.measured_delta_kva),
    ...
}
# Problem: Doesn't account for BASELINE MDI (what if no action was taken)
# Doesn't measure actual WINDOW MDI (only individual load reductions)
```

**Correct approach (window_outcomes):**
```python
# window_events captures the COUNTERFACTUAL:
window_event = window_events.get(window_id)
outcome = {
    "actual_mdi": window_event.actual_mdi,      # Measured (with actions)
    "baseline_mdi": window_event.baseline_mdi,  # Estimated (without actions)
    "realized_saving": f(actual_mdi, baseline_mdi, tariff)
}
# Result: True causal impact (projected vs realized)
```

**Why window_outcomes is unique**:
1. **Pairs actual vs counterfactual** - can't derive from single tables
2. **Window-aggregated context** - billing and cost calculations require window boundaries
3. **Facility + time grouping** - enables multi-facility time series learning
4. **Links to monthly_state** - enables cumulative peak tracking (needed for bill calculation)

---

## Part 6: Readiness Assessment for Model Training

### Current State: ❌ BLOCKED

```
Stage 4 (XGBoost, 90 days):
  ├─ window_outcomes table schema ✓ READY
  ├─ OutcomeEngine.compute() logic ✓ READY (but never called)
  ├─ Data in window_outcomes ✗ EMPTY (no processor)
  └─ Projection pairs for training ✗ NOT AVAILABLE
  
Stage 5 (RL + LightGBM, 6 months):
  ├─ window_outcomes table schema ✓ READY
  ├─ Reward signal (demand_saving) ✓ IN SCHEMA (but unfilled)
  ├─ Multi-facility aggregation ✓ DESIGNED (but no data)
  ├─ energy_saving field ✗ NOT IMPLEMENTED
  ├─ actual_bill field ✗ NOT IMPLEMENTED
  └─ Full training dataset ✗ NOT AVAILABLE
```

---

## Part 7: What's Missing (MVP Implementation Gaps)

### Missing Component: Window Outcome Processor

**Current**: ❌ No processor consumes `window_events`

**Needed**:
```python
# Pseudo-code for what OutcomeEngine should do (currently unused)

class WindowOutcomeProcessor:
    def process_window(self, conn, window_event):
        # 1. Get window data
        actual_mdi = window_event['actual_mdi']
        baseline_mdi = window_event['baseline_mdi']
        window_start = window_event['window_start']
        
        # 2. Load monthly state
        monthly_state = load_monthly_state(facility_id, billing_cycle)
        
        # 3. Compute realized economics
        realized_saving = compute_actual_saving(
            counterfactual_mdi=baseline_mdi,
            actual_mdi=actual_mdi,
            contract_demand=facility.contract_demand_kva,
            tariff=tariff,
            previous_peak=monthly_state.previous_peak
        )
        
        # 4. Update peaks
        new_actual_peak = max(monthly_state.actual_peak, actual_mdi)
        new_baseline_peak = max(monthly_state.baseline_peak, baseline_mdi)
        
        # 5. Insert to window_outcomes
        window_outcome = {
            "facility_id": facility_id,
            "window_start": window_start,
            "actual_mdi": round(actual_mdi, 2),
            "baseline_mdi": round(baseline_mdi, 2),
            "actual_peak": round(new_actual_peak, 2),
            "baseline_peak": round(new_baseline_peak, 2),
            "demand_saving": round(realized_saving, 2),
            "energy_saving": 0.0,  # TODO: implement TOD tariff impact
            "total_saving": round(realized_saving, 2),
            "window_kvah": window_event.get('accumulated_kvah', 0.0),
            "window_kwh": 0.0,  # TODO: implement
        }
        
        # 6. Write to WAL (two-phase)
        event_logger.log_outcome_by_window(window_outcome)
        
        # 7. Mark processed
        mark_window_processed(window_event.id)
```

---

## Part 8: Verdict for Future Model Training

### Is window_outcomes Important? ✓ **YES - ESSENTIAL**

| Aspect | Assessment |
|--------|-----------|
| **Unique data?** | ✓ YES - Only source of (actual_mdi, baseline_mdi) pairs |
| **ML training?** | ✓ YES - Both Stage 4 (supervised) and Stage 5 (RL) depend on it |
| **Grouping necessary?** | ✓ YES - Window + facility grouping enables time series learning |
| **Future scalability?** | ✓ YES - Indexes on (facility_id, window_start) enable multi-facility studies |
| **Implemented today?** | ✗ NO - OutcomeEngine not invoked, tables empty |

### What to Do Now (for future ML):

**Priority 1** (Before Stage 4):
- ✓ Implement `WindowOutcomeProcessor` background task
- ✓ Invoke `OutcomeEngine.compute()` for each `window_events` entry
- ✓ Populate `window_outcomes` table
- ✓ Collect 90 days of window_outcomes data

**Priority 2** (Before Stage 5):
- ✓ Implement `energy_saving` calculation (TOD tariff impact)
- ✓ Implement `actual_bill` and `baseline_bill` (cost tracking)
- ✓ Add `execution_confirmation_rate` join from outcome_events
- ✓ Link `load_performance_ratio` from confidence_scorer

**Priority 3** (Beyond Stage 5):
- ✓ Add `anomaly_flags` (for outlier detection in RL)
- ✓ Add `weather_context` (temperature, humidity for load modeling)
- ✓ Add `grid_events` (frequency, voltage anomalies)

---

## Summary

**window_outcomes is not just important - it's the foundation of future ML:**

1. **Causal ground truth** - Unique pairing of actual vs counterfactual outcomes
2. **Aggregation level** - Window-level granularity matches billing and forecasting needs
3. **Multi-facility learning** - Indexes enable cross-facility pattern discovery
4. **Training signals** - demand_saving provides RL rewards; projection errors enable supervised learning

**Current status**: ❌ Schema designed but **completely unused** due to missing processor pipeline.

**Action**: Implement WindowOutcomeProcessor to consume window_events and populate window_outcomes before Stage 4 ML modeling begins (requires 90+ days of data).

---
