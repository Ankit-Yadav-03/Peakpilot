# Data Flow Diagram: Window Tables Analysis

## Current Implementation (What Runs)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          METER TICK STREAM                              │
│                    (every 15 seconds, continuous)                       │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │
                             ▼
        ┌────────────────────────────────────────┐
        │      DecisionEngine.process_tick()     │
        └─────────────┬──────────────────────────┘
                      │
        ┌─────────────┴──────────────┐
        │                            │
        ▼                            ▼
   ┌─────────────────┐         ┌─────────────────────┐
   │  StateDetector  │         │ Risk/Cost/Tariff    │
   │  (30-min timer) │         │ Analysis            │
   └────────┬────────┘         └──────────┬──────────┘
            │                             │
            │ Window complete             │ Generate
            │ every 30 min                │ recommendation
            │                             │
            ▼                             ▼
   ┌─────────────────┐         ┌──────────────────┐
   │ _window_events  │         │ decision_events  │
   │ (async queue)   │         │ (database)       │
   └────────┬────────┘         └──────────┬───────┘
            │                             │
            ▼                             │
   ┌─────────────────────────────────────┼─────────┐
   │ log_window_event()                  │         │
   │ (EventLogger)                       │         │
   └──────────┬────────────────────────────┼───────┘
              │                            │
              ▼                            ▼
   ┌──────────────────┐          ┌──────────────────┐
   │  event_wal       │          │  event_wal       │
   │ (Phase 1: WAL)   │          │ (Phase 1: WAL)   │
   └────────┬─────────┘          └──────────┬───────┘
            │                               │
            └───────────┬───────────────────┘
                        │
                        ▼
        ┌──────────────────────────────────┐
        │  Background Flush Worker        │
        │  (every 2 seconds)              │
        └────────────┬─────────────────────┘
                     │
        ┌────────────┴───────────┐
        │                        │
        ▼                        ▼
   ┌──────────────┐        ┌──────────────┐
   │ window_events│        │execution_    │
   │   TABLE ✓    │        │events TABLE  │
   │              │        │      ✓       │
   │ (gets data)  │        │  (gets data) │
   └──────────────┘        └──────────┬───┘
                                      │
                                      ▼
                        ┌──────────────────────┐
                        │  OutcomeWorker       │
                        │  (background thread) │
                        └─────────────┬────────┘
                                      │
                    ┌─────────────────┴──────────────┐
                    │                                │
                    ▼                                ▼
            ┌──────────────┐            ┌──────────────────┐
            │   Fetch      │            │   Derive         │
            │ decision_    │   +        │ action_status    │
            │ events       │            │ from telemetry   │
            └──────────┬───┘            └──────────┬───────┘
                       │                           │
                       └────────────┬──────────────┘
                                    │
                                    ▼
                        ┌───────────────────────┐
                        │ outcome_events TABLE  │
                        │         ✓             │
                        │ (gets data)           │
                        └───────────────────────┘


MISSING: window_events → window_outcomes Pipeline
═════════════════════════════════════════════════════

    ┌──────────────────┐
    │ window_events    │         ❌ WHERE IS THIS PROCESSOR?
    │ TABLE (has data) │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ OutcomeEngine    │         ❌ .compute() NEVER CALLED
    │ .compute()       │
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ window_outcomes  │         ❌ TABLE STAYS EMPTY
    │    TABLE ✗       │
    └──────────────────┘


CURRENT STATE: OutcomeWorker Only Does Decision→Execution Reconciliation
═════════════════════════════════════════════════════════════════════════

    decision_events ────────┐
                            ├──→ OutcomeWorker ──→ outcome_events TABLE
    execution_events ───────┤
                            
    window_events ───────X (not processed)
```

## Data Sources vs. Writers

```
┌─────────────┬──────────────────┬──────────────┬──────────────────┐
│ TABLE       │ POPULATED BY     │ STATUS       │ WHEN POPULATED   │
├─────────────┼──────────────────┼──────────────┼──────────────────┤
│ window_     │ DecisionEngine   │ ✓ ACTIVE     │ Every 30 min     │
│ events      │ (via EventLogger)│              │ (window complete)│
├─────────────┼──────────────────┼──────────────┼──────────────────┤
│ window_     │ ??? NOBODY ???   │ ✗ UNUSED     │ NEVER            │
│ outcomes    │                  │              │                  │
├─────────────┼──────────────────┼──────────────┼──────────────────┤
│ decision_   │ DecisionEngine   │ ✓ ACTIVE     │ Every tick       │
│ events      │ (via EventLogger)│              │ if recommendation│
├─────────────┼──────────────────┼──────────────┼──────────────────┤
│ execution_  │ ExecutionManager │ ✓ ACTIVE     │ On each          │
│ events      │ (via EventLogger)│              │ load action      │
├─────────────┼──────────────────┼──────────────┼──────────────────┤
│ outcome_    │ OutcomeWorker    │ ✓ ACTIVE     │ When decision    │
│ events      │ (via EventLogger)│              │ reaches terminal │
│             │                  │              │ state            │
└─────────────┴──────────────────┴──────────────┴──────────────────┘
```

## What Each Table Stores

```
window_events (POPULATED ✓)
├─ window_start, window_end: Time boundaries of 30-min window
├─ actual_mdi: Measured MDI during window (from meter)
├─ baseline_mdi: Counterfactual MDI without intervention (estimated)
├─ accumulated_kvah: Energy consumed in window
└─ processed: Flag (0=new, 1=analyzed)

window_outcomes (EMPTY ✗)
├─ facility_id: Facility identifier
├─ window_start: Window reference
├─ actual_mdi, baseline_mdi, actual_peak, baseline_peak
├─ demand_saving: Realized rupees saved
├─ energy_saving: Not implemented (0.0)
├─ total_saving: demand_saving
├─ actual_bill, baseline_bill: Cost impact (not implemented)
└─ window_kvah, window_kwh: Energy metrics

decision_events (POPULATED ✓)
├─ Recommendations made by engine
├─ projected_mdi_kva, expected_reduction, projected_saving
├─ confidence score, loads selected
└─ trigger (DEMAND_RISK, TOD_OPTIMIZATION, SCHEDULED, SYSTEM)

execution_events (POPULATED ✓)
├─ Load shed/restore commands
├─ Confirmation status from equipment/meter telemetry
├─ Measured delta_kva (actual reduction achieved)
└─ Confirmation latency, source

outcome_events (POPULATED ✓)
├─ Reconciliation of decision with execution
├─ action_status: FOLLOWED / IGNORED / PARTIAL_CONFIRMATION
├─ projected vs measured reliability
└─ Links decision to execution chain
```

## Code References

| Module | File | Method | Action |
|--------|------|--------|--------|
| DecisionEngine | decision/decision_engine.py | _on_window_complete() | Detects window end |
| DecisionEngine | decision/decision_engine.py | process_tick() | Consumes _window_events queue |
| EventLogger | learning/event_logger.py | log_window_event() | Writes to event_wal |
| EventLogger | learning/event_logger.py | _flush_pending() | Flushes WAL to window_events |
| OutcomeEngine | outcome/outcome_engine.py | compute() | ❌ **NEVER CALLED** |
| OutcomeWorker | outcome/outcome_worker.py | run() | Processes decision→execution only |

---

## Why window_outcomes Table Is Empty

**Root Cause**: No processor is consuming `window_events` and calling `OutcomeEngine.compute()`.

**Evidence**: 
- `OutcomeEngine.compute()` has 0 invocations across codebase
- No references to reading `window_events` in outcome-related modules
- No conditional logic to process unfinalized windows

**To Fix**: Implement a background task that:
1. Polls `SELECT * FROM window_events WHERE processed = 0`
2. For each: Calls `outcome_engine.compute(conn, window_event)`
3. Writes result to `window_outcomes` via EventLogger or direct INSERT
4. Updates `processed = 1`

This could be integrated into:
- **OutcomeWorker** (as an additional loop after decision reconciliation)
- **New WindowOutcomeProcessor** (separate background task)
- **Nightly batch job** (if deferred calculation is acceptable)

---
