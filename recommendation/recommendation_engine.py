"""
recommendation/recommendation_engine.py

Formats final operator-facing recommendation.

Uses ActionStateManager as the single operational source of truth
for suppression, restoration visibility, and execution awareness.
"""
from __future__ import annotations
import logging
import uuid
from typing import List, Optional

from core.models import (
    RiskState, AnomalyState, TariffState, CostState,
    DemandRecommendation, TODRecommendation, ConflictResolution,
    ConfidenceResult, Recommendation,
)
from core.config import FacilityConfig
from decision.action_state_manager import ActionStateManager

logger = logging.getLogger(__name__)


def _load_name(load_id: str, facility_config: FacilityConfig) -> str:
    for l in facility_config.loads_raw.get("loads", []):
        if l["load_id"] == load_id:
            return l["name"]
    return load_id


def _blocked_operational_loads(
    action_manager: ActionStateManager,
    now,
) -> set[str]:

    return (
        set(action_manager.get_active_loads(now)) | set(action_manager.get_restore_pending_loads(now))
    )


def _calculate_actionable_impact(
    selected_loads: List[str],
    all_loads: List[str],
    total_reduction: float,
    total_saving: float,
) -> tuple[float, float]:

    if not all_loads:
        return 0.0, 0.0

    if not selected_loads:
        return 0.0, 0.0

    actionable_ratio = (
        len(selected_loads) / len(all_loads)
    )

    actionable_reduction = (
        total_reduction * actionable_ratio
    )

    actionable_saving = (
        total_saving * actionable_ratio
    )

    return (
        round(actionable_reduction, 2),
        round(actionable_saving, 2),
    )


def _format_demand_message(
    loads_selected: List[str],
    expected_reduction: float,
    expected_saving: float,
    risk_state: RiskState,
    cost_state: CostState,
    confidence_result: ConfidenceResult,
    facility_config: FacilityConfig,
) -> str:

    load_names = [
        _load_name(lid, facility_config)
        for lid in loads_selected
    ]

    lines = [
        f"⚡ DEMAND ALERT — {risk_state.risk_level}",
        f"",
        f"Projected MDI: {risk_state.projected_MDI_kva:.1f} kVA "
        f"(Contract: {risk_state.contract_demand_kva:.1f} kVA, "
        f"Headroom: {risk_state.headroom_kva:.1f} kVA)",

        f"Window: {risk_state.elapsed_minutes:.0f} min elapsed, "
        f"{risk_state.remaining_minutes:.0f} min remaining",

        f"",
        f"ACTION REQUIRED — Shed these loads NOW:",
    ]

    for i, (lid, name) in enumerate(
        zip(loads_selected, load_names),
        1
    ):
        lines.append(f"  {i}. {name} ({lid})")

    lines += [
        f"",
        f"Expected MDI reduction: "
        f"{expected_reduction:.1f} kVA",

        f"Projected avoidable demand charge: "
        f"₹{expected_saving:,.0f}",

        f"Projected monthly bill: "
        f"₹{cost_state.projected_monthly_bill:,.0f}",

        f"",
        f"Confidence: "
        f"{confidence_result.score:.0%} "
        f"[{confidence_result.display_action}]",
    ]

    if risk_state.escalation_reasons:
        lines.append(
            f"⚠ Escalations: "
            f"{'; '.join(risk_state.escalation_reasons)}"
        )

    if confidence_result.display_action in (
        "SHOW_LOW_WITH_FLAGS",
        "SHOW_HIGH_WITH_STRONG_FLAGS",
    ):
        lines.append(
            "⚠ Low confidence — verify conditions before acting."
        )

    return "\n".join(lines)


def _format_tod_message(
    loads_selected: List[str],
    expected_saving: float,
    tod_rec: TODRecommendation,
    tariff_state: TariffState,
    confidence_result: ConfidenceResult,
    facility_config: FacilityConfig,
) -> str:

    load_names = [
        _load_name(lid, facility_config)
        for lid in loads_selected
    ]

    action_label = {
        "SHIFT": "SHIFT LOADS",
        "PRE_RUN": "PRE-COOL / PRE-RUN",
        "NO_ACTION": "NO ACTION"
    }.get(tod_rec.action, tod_rec.action)

    lines = [
        f"🕐 TOD OPTIMIZATION — {action_label}",

        f"Current window: {tariff_state.tod_window}",

        f"",

        tod_rec.rationale,

        f"",

        f"Loads: "
        f"{', '.join(load_names) if load_names else 'None'}",

        f"Estimated saving: "
        f"₹{expected_saving:,.0f}",

        "",
        "Note: TOD recommendations are operational tariff "
        "optimization advisories only. Actual savings depend "
        "on execution timing and process behavior.",

        f"Confidence: "
        f"{confidence_result.score:.0%}",
    ]

    return "\n".join(lines)


def _format_no_action_message(
    risk_state: RiskState,
    tariff_state: TariffState,
    cost_state: CostState,
) -> str:
    return (
        f"✅ SAFE — No action required\n"
        f"Projected MDI: {risk_state.projected_MDI_kva:.1f} kVA "
        f"(Contract: {risk_state.contract_demand_kva:.1f} kVA)\n"
        f"TOD: {tariff_state.tod_window} | "
        f"Bill projection: ₹{cost_state.projected_monthly_bill:,.0f}/month"
    )


class RecommendationEngine:
    """
    Formats final operator recommendation.
    Uses ActionStateManager execution state to avoid duplicate operational recommendations.
    """

    def __init__(self, facility_config: FacilityConfig, action_manager: ActionStateManager,):
        self._facility = facility_config
        self._action_manager = action_manager

    def build(
        self,
        risk_state: RiskState,
        anomaly_state: AnomalyState,
        tariff_state: TariffState,
        cost_state: CostState,
        demand_rec: Optional[DemandRecommendation],
        tod_rec: Optional[TODRecommendation],
        conflict_resolution: Optional[ConflictResolution],
        confidence_result: ConfidenceResult,
    ) -> Recommendation:
        now = risk_state.timestamp
        decision_id = str(uuid.uuid4())

        # Determine winning recommendation type
        winning = conflict_resolution.winning_recommendation if conflict_resolution else "NONE"

        # Apply confidence suppression (already resolved in confidence_result)
        suppressed = confidence_result.suppressed
        suppression_reason = confidence_result.suppression_reason
        intelligent_override = False
        override_reason = None

        # Determine recommendation type and loads
        if suppressed and risk_state.risk_level == "CRITICAL":
            # NEVER suppress CRITICAL
            suppressed = False
            intelligent_override = True
            override_reason = "CRITICAL risk overrides confidence suppression."
            logger.warning(f"IntelligenceLayer override: CRITICAL risk, unsuppressing recommendation.")

        if winning == "DEMAND" and demand_rec and demand_rec.is_valid and not suppressed:

            rec_type = "SHED"

            planner_selected_loads = demand_rec.recommended_loads

            blocked_loads = _blocked_operational_loads(self._action_manager, now,)

            actionable_loads = [
                lid
                for lid in planner_selected_loads
                if lid not in blocked_loads
            ]

            if not actionable_loads:

                rec_type = "NO_ACTION"
                loads_selected = []
                expected_reduction = 0.0
                expected_saving = 0.0
                trigger = "SCHEDULED"

                message = (
                    "No action required: selected loads are already "
                    "under operational control or awaiting restoration."
                )

            else:

                loads_selected = actionable_loads

                economic_impact = demand_rec.economic_impact

                projected_saving = (
                    economic_impact.projected_saving_rupees
                    if economic_impact is not None
                    else 0.0
                )

                expected_reduction, expected_saving = (
                    _calculate_actionable_impact(
                        selected_loads=actionable_loads,
                        all_loads=planner_selected_loads,
                        total_reduction=demand_rec.expected_reduction_kva,
                        total_saving=projected_saving,
                    )
                )
                trigger = "DEMAND_RISK"

                message = _format_demand_message(
                    loads_selected,
                    expected_reduction,
                    expected_saving,
                    risk_state,
                    cost_state,
                    confidence_result,
                    self._facility
                )


        elif winning == "TOD" and tod_rec and tod_rec.action != "NO_ACTION" and not suppressed:

            rec_type = (
                "DELAY"
                if tod_rec.action == "SHIFT"
                else "PRE_RUN"
            )

            planner_selected_loads = tod_rec.loads

            blocked_loads = _blocked_operational_loads(self._action_manager, now,)

            actionable_loads = [
                lid
                for lid in planner_selected_loads
                if lid not in blocked_loads
            ]

            if not actionable_loads:

                rec_type = "NO_ACTION"
                loads_selected = []
                expected_reduction = 0.0
                expected_saving = 0.0
                trigger = "SCHEDULED"

                message = (
                    "TOD recommendation skipped: loads are already "
                    "under active control."
                )

            else:

                loads_selected = actionable_loads

                expected_reduction = 0.0
                _, expected_saving = (
                    _calculate_actionable_impact(
                        selected_loads=actionable_loads,
                        all_loads=planner_selected_loads,
                        total_reduction=0.0,
                        total_saving=tod_rec.estimated_saving_rupees,
                    )
                )
                trigger = "TOD_OPTIMIZATION"

                message = _format_tod_message(
                    loads_selected,
                    expected_saving,
                    tod_rec,
                    tariff_state,
                    confidence_result,
                    self._facility
                )


        else:
            rec_type = "NO_ACTION"
            loads_selected = []
            expected_reduction = 0.0
            expected_saving = 0.0
            trigger = "SCHEDULED"
            if suppressed:
                message = f"[SUPPRESSED] {suppression_reason or 'Low confidence.'}"
            else:
                message = _format_no_action_message(risk_state, tariff_state, cost_state)

        cost_breakdown = {
            "demand_charge": cost_state.demand_charge,
            "excess_surcharge": cost_state.excess_surcharge,
            "energy_charge": cost_state.energy_charge,
            "drrs": cost_state.drrs,
            "pension_trust": cost_state.pension_trust,
            "ppac": cost_state.ppac,
            "electricity_duty": cost_state.electricity_duty,
            "total_projected_bill": cost_state.projected_monthly_bill,
        }

        economic_impact = None

        if (
            demand_rec is not None
            and demand_rec.economic_impact is not None
        ):

            original = demand_rec.economic_impact

            economic_impact = original.__class__(
                prevented_md_kva=expected_reduction,

                projected_saving_rupees=expected_saving,

                economic_status=(
                    original.economic_status
                ),

                saving_basis=(
                    original.saving_basis
                ),
            )

        return Recommendation(
            timestamp=now,
            facility_id=self._facility.facility_id,
            decision_id=decision_id,
            recommendation_type=rec_type,
            risk_level=risk_state.risk_level,
            loads_selected=loads_selected,
            expected_mdi_reduction_kva=round(expected_reduction, 2),
            economic_impact=economic_impact,
            confidence=confidence_result.score,
            display_action=confidence_result.display_action,
            message=message,
            cost_breakdown=cost_breakdown,
            suppressed=suppressed,
            trigger=trigger,
            conflict_resolved=conflict_resolution is not None and conflict_resolution.winning_recommendation not in ("NONE", None),
            conflict_resolution_detail=conflict_resolution.resolution_reason if conflict_resolution else None,
            intelligent_layer_override=intelligent_override,
            override_reason=override_reason,
        )
