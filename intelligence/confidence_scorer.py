"""
intelligence/confidence_scorer.py
Six-signal weighted confidence scoring with familiarity-stratified compliance.
Weights and defaults exactly per spec.
"""
from __future__ import annotations
import logging
from collections import defaultdict, deque
from typing import Dict, Optional, Deque, Tuple

from core.models import (
    RiskState, AnomalyState, WindowState, TariffState,
    ConfidenceSignals, ConfidenceResult,
)

logger = logging.getLogger(__name__)

# Signal weights (must sum to 1.0)
WEIGHTS = {
    "projection_accuracy":     0.30,
    "execution_confirmation":  0.25,
    "load_performance":        0.20,
    "condition_familiarity":   0.15,
    "data_quality":            0.05,
    "billing_cycle_position":  0.05,
}

# Defaults when insufficient data
DEFAULTS = {
    "projection_accuracy":    0.60,
    "execution_confirmation": 0.70,
    "load_performance":       0.70,
    "condition_familiarity":  0.60,
    "billing_cycle_position": 0.60,
}

# Familiarity bucket thresholds
FAMILIARITY_LOW_MAX = 0.5
FAMILIARITY_HIGH_MIN = 0.75

# Urgency/display thresholds
URGENCY_THRESHOLDS = [
    # (confidence_min, risk_required, display_action, suppress)
    (0.75, None,       "SHOW_HIGH",               False),
    (0.55, None,       "SHOW_MEDIUM",              False),
    (0.35, None,       "SHOW_LOW_WITH_FLAGS",      False),
]


def _familiarity_bucket(score: float) -> str:
    if score < FAMILIARITY_LOW_MAX:
        return "LOW"
    elif score < FAMILIARITY_HIGH_MIN:
        return "MEDIUM"
    else:
        return "HIGH"


def _data_quality_score(data_quality: str) -> float:
    if data_quality == "GOOD":
        return 1.0
    elif data_quality == "INTERPOLATED":
        return 0.7
    else:  # STALE
        return 0.3


def _billing_cycle_position_score(billing_cycle_day: int) -> float:
    """Higher score = higher confidence in decision quality.
    Days 1-3: highest risk, highest attention → 0.9
    Days 4-10: normal → 0.7
    Days 11-20: MD likely set, TOD decisions more relevant → 0.6
    Days 21+: end of cycle → 0.5
    """
    if billing_cycle_day <= 3:
        return 0.90
    elif billing_cycle_day <= 10:
        return 0.70
    elif billing_cycle_day <= 20:
        return 0.60
    else:
        return 0.50


class ConfidenceScorer:
    """
    Maintains rolling history for:
      - Projection accuracy (projected vs actual MDI per window)
      - Operator compliance (stratified by familiarity bucket)
      - Load performance ratio
      - Condition familiarity (recurrence of similar conditions)
    """

    def __init__(self, history_window: int = 90):
        """history_window: number of past windows to consider."""
        self._history_window = history_window

        # projection_error: (projected_mdi, actual_mdi) pairs
        self._projection_history: Deque[Tuple[float, float]] = deque(maxlen=history_window)

        # Operator compliance per familiarity bucket:
        # {bucket: deque of 0/1 (1=followed, 0=ignored)}
        self._execution_confirmation_by_bucket: Dict[str, Deque[int]] = defaultdict(lambda: deque(maxlen=history_window))

        # Load performance ratios: {load_id: deque of ratios}
        self._load_perf_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=history_window))

        # Condition history: list of (tod_window, risk_level, billing_day_bucket)
        self._condition_history: Deque[Tuple[str, str, str]] = deque(maxlen=history_window)

    # -----------------------------------------------------------------------
    # Feed methods (call from calibration / event logger on outcomes)
    # -----------------------------------------------------------------------

    def record_projection_outcome(self, projected_mdi: float, actual_mdi: float) -> None:
        self._projection_history.append((projected_mdi, actual_mdi))

    def record_execution_confirmation(self, confirmed: bool, familiarity_score: float) -> None:
        bucket = _familiarity_bucket(familiarity_score)
        self._execution_confirmation_by_bucket[bucket].append(1 if confirmed else 0)

    def record_load_performance(self, load_id: str, ratio: float) -> None:
        self._load_perf_history[load_id].append(ratio)

    def record_condition(self, tod_window: str, risk_level: str, billing_cycle_day: int) -> None:
        billing_bucket = "EARLY" if billing_cycle_day <= 5 else ("MID" if billing_cycle_day <= 15 else "LATE")
        self._condition_history.append((tod_window, risk_level, billing_bucket))

    # -----------------------------------------------------------------------
    # Score computation
    # -----------------------------------------------------------------------

    def _projection_accuracy_score(self) -> float:
        if len(self._projection_history) < 5:
            return DEFAULTS["projection_accuracy"]
        errors = []
        for proj, actual in self._projection_history:
            if actual > 0:
                pct_err = abs(proj - actual) / actual
                errors.append(pct_err)
        if not errors:
            return DEFAULTS["projection_accuracy"]
        mean_err = sum(errors) / len(errors)
        # 0% error → 1.0, 50% error → 0.0
        return max(0.0, min(1.0, 1.0 - mean_err * 2.0))

    def _execution_confirmation_score(self, current_familiarity_score: float) -> float:
        """
        Stratified by current familiarity bucket.
        Breaks correlation between compliance and familiarity signals.
        """
        bucket = _familiarity_bucket(current_familiarity_score)
        history = self._execution_confirmation_by_bucket[bucket]
        if len(history) < 3:
            return DEFAULTS["execution_confirmation"]
        return sum(history) / len(history)

    def _load_performance_score(self) -> float:
        all_ratios = []
        for ratios in self._load_perf_history.values():
            all_ratios.extend(ratios)
        if len(all_ratios) < 5:
            return DEFAULTS["load_performance"]
        mean_ratio = sum(all_ratios) / len(all_ratios)
        # ratio=1.0 → perfect, ratio=0.5 → poor
        return max(0.0, min(1.0, mean_ratio))

    def _condition_familiarity_score(
        self, tod_window: str, risk_level: str, billing_cycle_day: int
    ) -> float:
        if len(self._condition_history) < 5:
            return DEFAULTS["condition_familiarity"]
        billing_bucket = "EARLY" if billing_cycle_day <= 5 else ("MID" if billing_cycle_day <= 15 else "LATE")
        target = (tod_window, risk_level, billing_bucket)
        matches = sum(1 for c in self._condition_history if c == target)
        # Familiarity = fraction of history that matches this condition
        score = matches / len(self._condition_history)
        # Scale: 10% recurrence → 0.6, 50% → 1.0
        scaled = 0.5 + score * 1.0
        return max(0.0, min(1.0, scaled))

    def compute(
        self,
        risk_state: RiskState,
        anomaly_state: AnomalyState,
        tariff_state: TariffState,
    ) -> ConfidenceResult:
        # Compute all six signals
        cond_fam = self._condition_familiarity_score(
            tariff_state.tod_window,
            risk_state.risk_level,
            risk_state.billing_cycle_day,
        )

        signals = ConfidenceSignals(
            projection_accuracy_score=self._projection_accuracy_score(),
            execution_confirmation_score=self._execution_confirmation_score(cond_fam),
            load_performance_score=self._load_performance_score(),
            condition_familiarity_score=cond_fam,
            data_quality_score=_data_quality_score(
                risk_state.timestamp and "GOOD"
                if anomaly_state.stale_data_detected is False else "STALE"
            ),
            billing_cycle_position_score=_billing_cycle_position_score(risk_state.billing_cycle_day),
        )

        # Override data quality if stale
        if anomaly_state.stale_data_detected:
            signals.data_quality_score = 0.3

        # Weighted sum
        raw_score = (
            WEIGHTS["projection_accuracy"]    * signals.projection_accuracy_score
            + WEIGHTS["execution_confirmation"]  * signals.execution_confirmation_score
            + WEIGHTS["load_performance"]     * signals.load_performance_score
            + WEIGHTS["condition_familiarity"]* signals.condition_familiarity_score
            + WEIGHTS["data_quality"]         * signals.data_quality_score
            + WEIGHTS["billing_cycle_position"]* signals.billing_cycle_position_score
        )

        # Apply anomaly caps
        if anomaly_state.confidence_cap is not None:
            score = min(raw_score, anomaly_state.confidence_cap)
        else:
            score = raw_score

        score = round(max(0.0, min(1.0, score)), 4)

        familiarity_bucket = _familiarity_bucket(cond_fam)
        risk_level = risk_state.risk_level

        # Record condition for future familiarity
        self.record_condition(tariff_state.tod_window, risk_level, risk_state.billing_cycle_day)

        # Determine display action per spec urgency table
        display_action, suppressed, suppression_reason = self._resolve_display_action(score, risk_level)

        logger.debug(
            f"ConfidenceScorer: score={score:.3f}, bucket={familiarity_bucket}, "
            f"action={display_action}, risk={risk_level}"
        )

        return ConfidenceResult(
            score=score,
            signals=signals,
            familiarity_bucket=familiarity_bucket,
            display_action=display_action,
            suppressed=suppressed,
            suppression_reason=suppression_reason,
        )

    def _resolve_display_action(
        self, score: float, risk_level: str
    ) -> Tuple[str, bool, Optional[str]]:
        """
        Urgency threshold table from spec:
        ≥0.75 any           → SHOW_HIGH
        ≥0.55 any           → SHOW_MEDIUM
        ≥0.35 any           → SHOW_LOW_WITH_FLAGS
        <0.35 non-CRITICAL  → SUPPRESS_LOG_ONLY
        <0.45 non-CRITICAL  → suppress (confidence floor)
        ≥0.50 CRITICAL      → SHOW_HIGH (override)
        <0.50 CRITICAL      → SHOW_HIGH_WITH_STRONG_FLAGS — never suppress CRITICAL
        """
        is_critical = risk_level == "CRITICAL"

        if is_critical:
            if score >= 0.50:
                return "SHOW_HIGH", False, None
            else:
                return "SHOW_HIGH_WITH_STRONG_FLAGS", False, None

        # Non-CRITICAL suppression rules
        if score < 0.35:
            return "SUPPRESS_LOG_ONLY", True, f"Confidence {score:.3f} < 0.35 floor."
        if score < 0.45:
            return "SUPPRESS_LOG_ONLY", True, f"Confidence {score:.3f} < 0.45 floor."

        if score >= 0.75:
            return "SHOW_HIGH", False, None
        elif score >= 0.55:
            return "SHOW_MEDIUM", False, None
        elif score >= 0.35:
            return "SHOW_LOW_WITH_FLAGS", False, None

        return "SUPPRESS_LOG_ONLY", True, f"Confidence {score:.3f} below all thresholds."
