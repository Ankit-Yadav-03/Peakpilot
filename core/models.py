"""
core/models.py
All dataclasses for the Peakpilot pipeline.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any


# ---------------------------------------------------------------------------
# Ingestion Layer
# ---------------------------------------------------------------------------

@dataclass
class MeterTick:
    timestamp: datetime
    facility_id: str
    kw: float
    kva: float
    kvar: float
    pf: float
    frequency: float
    kvah_cumulative: float          # Running total, never resets mid-month
    voltage_l1: float
    voltage_l2: float
    voltage_l3: float
    source: str                     # MODBUS / MQTT / SIMULATION
    data_quality: str               # GOOD / INTERPOLATED / STALE
    polling_latency_ms: float = 0.0


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# State Detection
# ---------------------------------------------------------------------------

@dataclass
class WindowState:
    window_start: datetime
    window_end: datetime
    elapsed_minutes: float
    remaining_minutes: float
    accumulated_kVAh: float
    last_tick: Optional[MeterTick] = None
    tick_count: int = 0


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------

@dataclass
class TariffState:
    timestamp: datetime
    tod_window: str                 # PEAK / OFF_PEAK / NORMAL / NO_TOD
    effective_energy_rate: float    # ₹/kVAh after TOD and rebate
    base_energy_rate: float
    tod_multiplier: float
    voltage_rebate_factor: float    # e.g. 0.97 for 11kV
    is_tod_applicable: bool
    month: int


# ---------------------------------------------------------------------------
# Cost Simulation
# ---------------------------------------------------------------------------

@dataclass
class CostState:
    timestamp: datetime
    instantaneous_cost_rate_per_hour: float     # ₹/hr at current consumption
    projected_monthly_bill: float               # ₹
    demand_charge: float
    excess_surcharge: float
    energy_charge: float
    drrs: float
    pension_trust: float
    ppac: float
    electricity_duty: float
    total_bill: float
    projected_mdi_kva: float
    contract_demand_kva: float
    excess_cost: float


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

@dataclass
class RiskState:
    timestamp: datetime
    window_start: datetime
    elapsed_minutes: float
    remaining_minutes: float
    accumulated_kVAh: float
    projected_MDI_kva: float
    contract_demand_kva: float
    headroom_kva: float
    risk_level: str                 # SAFE / WATCH / WARNING / CRITICAL
    months_MD_so_far_kva: float
    will_set_new_monthly_MD: bool
    billing_cycle_day: int
    escalation_reasons: List[str] = field(default_factory=list)


@dataclass
class AnomalyState:
    timestamp: datetime
    inrush_detected: bool
    inrush_suppression_active: bool
    load_creep_detected: bool
    load_creep_consecutive_windows: int
    stale_data_detected: bool
    confidence_cap: Optional[float]             # None if no cap applied
    anomaly_flags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

@dataclass
class ShiftableLoad:
    load_id: str
    name: str
    typical_kw: float
    typical_pf: float
    shiftable: bool
    max_delay_minutes: int
    max_delay_confidence: str       # LOW / MEDIUM / HIGH
    restart_penalty_minutes: int
    priority: int                   # 1=never, 2=last, 3=first
    process_dependency: List[str]
    startup_dependency: List[str]
    thermal_time_constant_minutes: int = 0

    @property
    def typical_kva(self) -> float:
        return self.typical_kw / max(self.typical_pf, 0.01)

    @property
    def effective_max_delay_minutes(self) -> int:
        """Apply LOW confidence penalty per spec."""
        if self.max_delay_confidence == "LOW":
            return int(self.max_delay_minutes * 0.5)
        return self.max_delay_minutes


@dataclass
class ShedCandidate:
    load: ShiftableLoad
    mdi_impact_kva: float           # kVA reduction if shed now
    shed_score: float
    remaining_minutes: float


@dataclass
class MonthlyEconomicState:
    billing_cycle: str
    actual_monthly_peak_kva: float
    uncontrolled_monthly_peak_kva: float
    protected_peak_delta_kva: float
    last_updated: datetime


@dataclass
class EconomicImpact:
    prevented_md_kva: float
    projected_saving_rupees: float
    economic_status: str
    saving_basis: str


@dataclass
class DemandRecommendation:
    timestamp: datetime
    recommended_loads: List[str]    # load_ids
    required_reduction_kva: float
    expected_reduction_kva: float
    economic_impact: Optional[EconomicImpact]
    counterfactual_mdi_kva: float   # Locked at decision time
    risk_level: str
    dependency_violations: List[str]
    is_valid: bool


@dataclass
class TODRecommendation:
    timestamp: datetime
    action: str                     # SHIFT / PRE_RUN / NO_ACTION
    loads: List[str]
    estimated_saving_rupees: float
    rationale: str
    pre_cooling_applicable: bool


@dataclass
class ConflictResolution:
    timestamp: datetime
    winning_recommendation: str     # DEMAND / TOD / NONE
    demand_saving: float
    tod_saving: float
    multiplier_applied: float
    billing_cycle_day: int
    resolution_reason: str


# ---------------------------------------------------------------------------
# Intelligence Layer
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceSignals:
    projection_accuracy_score: float
    execution_confirmation_score: float     # Stratified by familiarity bucket
    load_performance_score: float
    condition_familiarity_score: float
    data_quality_score: float
    billing_cycle_position_score: float


@dataclass
class ConfidenceResult:
    score: float
    signals: ConfidenceSignals
    familiarity_bucket: str         # LOW / MEDIUM / HIGH
    display_action: str             # SHOW_HIGH / SHOW_MEDIUM / SHOW_LOW_WITH_FLAGS / SUPPRESS_LOG_ONLY / SHOW_HIGH_WITH_STRONG_FLAGS
    suppressed: bool
    suppression_reason: Optional[str]


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    timestamp: datetime
    facility_id: str
    decision_id: str
    recommendation_type: str        # SHED / RESTORE / DELAY / PRE_RUN / NO_ACTION
    risk_level: str
    loads_selected: List[str]
    expected_mdi_reduction_kva: float
    economic_impact: Optional[EconomicImpact]
    confidence: float
    display_action: str
    message: str                    # Operator-facing formatted message
    cost_breakdown: Dict[str, float]
    suppressed: bool
    trigger: str                    # DEMAND_RISK / TOD_OPTIMIZATION / SCHEDULED
    conflict_resolved: bool
    conflict_resolution_detail: Optional[str]
    intelligent_layer_override: bool
    override_reason: Optional[str]
    meta: dict | None = None


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationRecord:
    facility_id: str
    load_id: str
    expected_kva: float
    actual_kva: float
    load_performance_ratio: float
    window_count: int
    regime_change_detected: bool
    flagged_for_review: bool
    calibration_timestamp: datetime


@dataclass
class ProjectionBiasRecord:
    facility_id: str
    hour_of_day: int
    error_sum: float
    sample_count: int
    correction_factor: float
    calibration_timestamp: datetime


# ---------------------------------------------------------------------------
# Pipeline Output (complete per-tick result)
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    tick: MeterTick
    validation: ValidationResult
    window_state: Optional[WindowState]
    tariff_state: Optional[TariffState]
    cost_state: Optional[CostState]
    risk_state: Optional[RiskState]
    anomaly_state: Optional[AnomalyState]
    demand_recommendation: Optional[DemandRecommendation]
    tod_recommendation: Optional[TODRecommendation]
    conflict_resolution: Optional[ConflictResolution]
    confidence_result: Optional[ConfidenceResult]
    final_recommendation: Optional[Recommendation]
    pipeline_errors: List[str] = field(default_factory=list)
