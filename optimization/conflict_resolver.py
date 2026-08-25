"""
optimization/conflict_resolver.py
Adaptive conflict multiplier based on billing cycle day.
Demand protection vs TOD saving priority resolution.
"""
from __future__ import annotations
import logging
from typing import Optional

from core.models import DemandRecommendation, TODRecommendation, ConflictResolution, RiskState

logger = logging.getLogger(__name__)


def _tod_allowed_priority(
    billing_cycle_day: int,
) -> bool:

    # Early billing cycle:
    # demand protection dominates.

    return billing_cycle_day > 15


def _compatibility_multiplier() -> float:
    """
    Legacy compatibility field retained for:
    - schema stability
    - API stability
    - historical analytics compatibility

    No longer used for economic arbitration.
    """
    return 1.0


class ConflictResolver:
    """
    Merges demand and TOD recommendations by priority.
    Demand protection is always primary.
    TOD optimization is advisory-only and may run
    only when demand risk is operationally safe.
    """

    def resolve(
        self,
        demand_rec: Optional[DemandRecommendation],
        tod_rec: Optional[TODRecommendation],
        risk_state: RiskState,
    ) -> ConflictResolution:
        billing_day = risk_state.billing_cycle_day

        multiplier = _compatibility_multiplier()

        demand_saving = (
            demand_rec.economic_impact.projected_saving_rupees
            if (
                demand_rec
                and demand_rec.is_valid
                and demand_rec.economic_impact is not None
            )
            else 0.0
        )

        tod_saving = (
            tod_rec.estimated_saving_rupees
            if (
                tod_rec
                and tod_rec.action != "NO_ACTION"
            )
            else 0.0
        )

        tod_priority_allowed = _tod_allowed_priority(
            billing_day
        )

        # Case: only demand recommendation
        if demand_rec and demand_rec.is_valid and (not tod_rec or tod_rec.action == "NO_ACTION"):
            return ConflictResolution(
                timestamp=risk_state.timestamp,
                winning_recommendation="DEMAND",
                demand_saving=demand_saving,
                tod_saving=tod_saving,
                multiplier_applied=multiplier,
                billing_cycle_day=billing_day,
                resolution_reason="Demand recommendation only; no conflicting TOD action.",
            )

        # Case: only TOD recommendation
        if (not demand_rec or not demand_rec.is_valid) and tod_rec and tod_rec.action != "NO_ACTION":
            return ConflictResolution(
                timestamp=risk_state.timestamp,
                winning_recommendation="TOD",
                demand_saving=demand_saving,
                tod_saving=tod_saving,
                multiplier_applied=multiplier,
                billing_cycle_day=billing_day,
                resolution_reason="TOD recommendation only; no demand risk action needed.",
            )

        # Case: both exist

        if (
            demand_rec
            and demand_rec.is_valid
            and tod_rec
            and tod_rec.action != "NO_ACTION"
        ):

            # Demand protection always dominates
            # during elevated risk.

            if risk_state.risk_level in (
                "WARNING",
                "CRITICAL",
            ):

                winner = "DEMAND"

                reason = (
                    "Demand protection prioritized due to elevated "
                    f"MDI risk level ({risk_state.risk_level})."
                )

            elif not tod_priority_allowed:

                winner = "DEMAND"

                reason = (
                    f"Demand protection prioritized during early "
                    f"billing cycle (day {billing_day})."
                )

            else:

                winner = "TOD"

                reason = (
                    "TOD operational optimization allowed because "
                    "MDI risk is not elevated and billing cycle "
                    "is beyond primary MD protection phase."
                )

            logger.info(f"ConflictResolver: {winner} wins. {reason}")
            return ConflictResolution(
                timestamp=risk_state.timestamp,
                winning_recommendation=winner,
                demand_saving=demand_saving,
                tod_saving=tod_saving,
                multiplier_applied=multiplier,
                billing_cycle_day=billing_day,
                resolution_reason=reason,
            )

        # Case: neither
        return ConflictResolution(
            timestamp=risk_state.timestamp,
            winning_recommendation="NONE",
            demand_saving=0.0,
            tod_saving=0.0,
            multiplier_applied=multiplier,
            billing_cycle_day=billing_day,
            resolution_reason="No actionable recommendations from either engine.",
        )
