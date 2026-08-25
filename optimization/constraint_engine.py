"""
optimization/constraint_engine.py
Load selection for demand reduction.
Shed score: (priority × 10) + kva_contribution - (restart_penalty_minutes × 0.5)
Priority 1 loads: NEVER selected.
Dependency validation: never shed a load while a dependent runs.
MDI impact formula: load_kva × (remaining_minutes / 30)
"""
from __future__ import annotations
import logging
from typing import List, Dict, Tuple, Any

from core.models import ShiftableLoad, DemandRecommendation, ShedCandidate, RiskState, EconomicImpact
from core.config import TariffConfig, FacilityConfig
from cost.cost_simulator import compute_projected_saving

logger = logging.getLogger(__name__)


def compute_required_reduction(
    projected_mdi: float,
    contract_demand: float,
    safety_margin: float,
) -> float:
    """Compute kVA reduction required to bring projected MDI within safe band."""
    target_mdi = contract_demand - safety_margin
    return max(0.0, projected_mdi - target_mdi)


def compute_mdi_impact(load_kva: float, remaining_minutes: float) -> float:
    """
    MDI impact of shedding a load now.
    MDI_impact = load_kva × (remaining_minutes / 30)
    """
    return load_kva * (remaining_minutes / 30.0)


def compute_shed_score(
    priority: int,
    kva_contribution: float,
    restart_penalty_minutes: int,
) -> float:
    """
    Shed score (higher → shed first):
    shed_score = (priority × 10) + kva_contribution - (restart_penalty_minutes × 0.5)
    Sort: priority 3 first, highest kVA first within tier.
    """
    return (priority * 10) + kva_contribution - (restart_penalty_minutes * 0.5)


def validate_shed_plan(
    selected_load_ids: List[str],
    all_loads: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """
    Validate dependency constraints.
    A load cannot be shed if another NON-SHED load depends on it (process_dependency).
    Returns (is_valid, violations).
    """
    violations = []
    loads_dict = {l["load_id"]: l for l in all_loads}
    shed_set = set(selected_load_ids)

    for load_id in selected_load_ids:
        for other_id, other_load in loads_dict.items():
            if load_id in other_load.get("process_dependency", []):
                if other_id not in shed_set:
                    violations.append(
                        f"Cannot shed {load_id} ({loads_dict.get(load_id, {}).get('name', '')}) — "
                        f"required by {other_id} ({other_load.get('name', '')}) which remains running."
                    )
    return (len(violations) == 0, violations)


class ConstraintEngine:
    """
    Selects loads to shed to meet required MDI reduction.
    Greedy selection by shed_score (accurate for ≤8 shiftable loads per spec).
    Priority 1 loads are never selected.
    Validates process dependency constraints.
    """

    def __init__(self, facility_config: FacilityConfig, tariff_config: TariffConfig):
        self._facility = facility_config
        self._tariff = tariff_config
        self._loads_raw: List[Dict] = facility_config.loads_raw.get("loads", [])

    # Note:
    # select_loads_to_shed → legacy greedy fallback
    # evaluate_load_combination → used by IntelligentPlanner

    def _build_shiftable_loads(self) -> List[ShiftableLoad]:
        loads = []
        for raw in self._loads_raw:
            sl = ShiftableLoad(
                load_id=raw["load_id"],
                name=raw["name"],
                typical_kw=raw["typical_kw"],
                typical_pf=raw["typical_pf"],
                shiftable=raw["shiftable"],
                max_delay_minutes=raw["max_delay_minutes"],
                max_delay_confidence=raw["max_delay_confidence"],
                restart_penalty_minutes=raw["restart_penalty_minutes"],
                priority=raw["priority"],
                process_dependency=raw.get("process_dependency", []),
                startup_dependency=raw.get("startup_dependency", []),
                thermal_time_constant_minutes=raw.get("thermal_time_constant_minutes", 0),
            )
            loads.append(sl)
        return loads
    
    def get_available_loads(self) -> List[ShiftableLoad]:
        """
        Returns shiftable, non-critical loads usable by planner.
        """

        loads = self._build_shiftable_loads()

        return [
            l for l in loads
            if l.shiftable
            and l.priority > 1
            and l.max_delay_minutes > 0
        ]

    def select_loads_to_shed(
        self,
        required_reduction_kva: float,
        remaining_minutes: float,
        risk_state: RiskState,
        available_loads_override: List[ShiftableLoad] | None = None,
    ) -> DemandRecommendation:
        """
        Select minimum set of loads to meet required_reduction_kva.
        Returns DemandRecommendation with projected operational and economic impact.
        """
        if available_loads_override is not None:
            all_loads = available_loads_override
        else:
            all_loads = self._build_shiftable_loads()

        # Candidates: shiftable=True, priority > 1, effective_max_delay > 0
        candidates: List[ShedCandidate] = []
        for load in all_loads:
            if not load.shiftable:
                continue
            if load.priority == 1:
                continue
            if load.effective_max_delay_minutes <= 0:
                continue

            mdi_impact = compute_mdi_impact(load.typical_kva, remaining_minutes)
            score = compute_shed_score(load.priority, load.typical_kva, load.restart_penalty_minutes)
            candidates.append(ShedCandidate(
                load=load,
                mdi_impact_kva=mdi_impact,
                shed_score=score,
                remaining_minutes=remaining_minutes,
            ))

        # Exclude loads that currently support active dependent processes.
        # Example:
        # Do not shed Air Compressor A while molding machines still depend on it.

        candidate_ids = {c.load.load_id for c in candidates}

        safe_candidates = []

        for cand in candidates:

            load_id = cand.load.load_id

            dependent_active = False

            for other in all_loads:

                if (
                    load_id in other.process_dependency
                    and other.load_id not in candidate_ids
                ):
                    dependent_active = True
                    break

            if dependent_active:
                logger.info(
                    f"ConstraintEngine: excluding {load_id} "
                    f"because active dependent process exists."
                )
                continue

            safe_candidates.append(cand)

        candidates = safe_candidates

        # Sort: priority descending, then kVA descending within same priority
        candidates.sort(key=lambda c: (-c.load.priority, -c.load.typical_kva))

        selected_ids: List[str] = []
        accumulated_reduction = 0.0

        for cand in candidates:
            if accumulated_reduction >= required_reduction_kva:
                break
            selected_ids.append(cand.load.load_id)
            accumulated_reduction += cand.mdi_impact_kva

        # Dependency validation
        is_valid, violations = validate_shed_plan(selected_ids, self._loads_raw)

        # Compute expected saving
        counterfactual_mdi = risk_state.projected_MDI_kva
        expected_mdi_after_shed = risk_state.projected_MDI_kva - accumulated_reduction
        contract = self._facility.contract_demand_kva

        try:
            expected_saving = compute_projected_saving(
                counterfactual_mdi=counterfactual_mdi,
                protected_mdi=expected_mdi_after_shed,
                contract_demand_kva=contract,
                tariff=self._tariff,
            )
        except ValueError as e:
            logger.error(f"Saving computation error: {e}")
            expected_saving = 0.0

        economic_impact = EconomicImpact(
            prevented_md_kva=round(accumulated_reduction, 2),
            projected_saving_rupees=round(expected_saving, 2),
            economic_status=(
                "PROJECTED_SAVING"
                if expected_saving > 0
                else "NO_PROJECTED_SAVING"
            ),
            saving_basis=(
                "PROJECTED_DEMAND_CHARGE_AVOIDANCE"
            ),
        )

        logger.info(
            f"ConstraintEngine: required={required_reduction_kva:.1f} kVA, "
            f"selected={selected_ids}, expected_reduction={accumulated_reduction:.1f} kVA, "
            f"saving=₹{expected_saving:.0f}, valid={is_valid}"
        )

        return DemandRecommendation(
            timestamp=risk_state.timestamp,
            recommended_loads=selected_ids,
            required_reduction_kva=round(required_reduction_kva, 2),
            expected_reduction_kva=round(accumulated_reduction, 2),
            economic_impact=economic_impact,
            counterfactual_mdi_kva=round(counterfactual_mdi, 2),
            risk_level=risk_state.risk_level,
            dependency_violations=violations,
            is_valid=is_valid,
        )
    
    def evaluate_load_combination(
        self,
        loads: List[ShiftableLoad],
        required_reduction_kva: float,
        risk_state: RiskState
    ) -> DemandRecommendation:
        """
        Evaluate a specific combination of loads (used by planner).
        """

        remaining_minutes = risk_state.remaining_minutes

        selected_ids = [l.load_id for l in loads]

        # 🔴 dependency validation
        is_valid, violations = validate_shed_plan(selected_ids, self._loads_raw)

        if not is_valid:
            return DemandRecommendation(
                timestamp=risk_state.timestamp,
                recommended_loads=selected_ids,
                required_reduction_kva=required_reduction_kva,
                expected_reduction_kva=0.0,
                economic_impact=None,
                counterfactual_mdi_kva=risk_state.projected_MDI_kva,
                risk_level=risk_state.risk_level,
                dependency_violations=violations,
                is_valid=False,
            )
        
        # 🔴 compute reduction
        accumulated_reduction = sum(
            compute_mdi_impact(l.typical_kva, remaining_minutes)
            for l in loads
        )

        # 🔴 compute saving
        counterfactual_mdi = risk_state.projected_MDI_kva
        expected_mdi_after = counterfactual_mdi - accumulated_reduction
        contract = self._facility.contract_demand_kva

        try:
            expected_saving = compute_projected_saving(
                counterfactual_mdi=counterfactual_mdi,
                protected_mdi=expected_mdi_after,
                contract_demand_kva=contract,
                tariff=self._tariff,
            )
        except Exception:
            expected_saving = 0.0

        economic_impact = EconomicImpact(
            prevented_md_kva=round(accumulated_reduction, 2),
            projected_saving_rupees=round(expected_saving, 2),
            economic_status=(
                "PROJECTED_SAVING"
                if expected_saving > 0
                else "NO_PROJECTED_SAVING"
            ),
            saving_basis=(
                "PROJECTED_DEMAND_CHARGE_AVOIDANCE"
            ),
        )

        return DemandRecommendation(
            timestamp=risk_state.timestamp,
            recommended_loads=selected_ids,
            required_reduction_kva=round(required_reduction_kva, 2),
            expected_reduction_kva=round(accumulated_reduction, 2),
            economic_impact=economic_impact,
            counterfactual_mdi_kva=round(counterfactual_mdi, 2),
            risk_level=risk_state.risk_level,
            dependency_violations=violations,
            is_valid=True,
        )