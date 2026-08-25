from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from core.models import MeterTick, WindowState, AnomalyState, RiskState
from core.config import FacilityConfig, SystemConfig

logger = logging.getLogger(__name__)


def _base_risk_level(ratio: float) -> str:
    if ratio < 0.70:
        return "SAFE"
    elif ratio < 0.80:
        return "WATCH"
    elif ratio < 0.90:
        return "WARNING"
    else:
        return "CRITICAL"


_RISK_ORDER = {"SAFE": 0, "WATCH": 1, "WARNING": 2, "CRITICAL": 3}
_RISK_LEVELS = ["SAFE", "WATCH", "WARNING", "CRITICAL"]


def _escalate(current: str) -> str:
    idx = _RISK_ORDER[current]
    return _RISK_LEVELS[min(idx + 1, 3)]


def _is_shift_changeover(ts: datetime, facility_config: FacilityConfig) -> bool:
    tod_minutes = ts.hour * 60 + ts.minute
    ss = facility_config.shift_schedule
    for shift_str in [ss.shift_1_start, ss.shift_2_start]:
        h, m = shift_str.split(":")
        start_min = int(h) * 60 + int(m)
        if abs(tod_minutes - start_min) <= 15:
            return True
    return False


class RiskEstimator:
    def __init__(self, facility_config: FacilityConfig, system_config: SystemConfig):
        self._facility = facility_config
        self._system = system_config
        self._monthly_md_kva: float = 0.0

        # 🔴 FIX: Track billing cycle
        self._current_cycle: Optional[str] = None

    def _check_cycle_reset(self, ts: datetime):
        cycle = ts.strftime("%Y-%m")
        if self._current_cycle is None:
            self._current_cycle = cycle
        elif cycle != self._current_cycle:
            logger.info(f"Billing cycle changed: {self._current_cycle} → {cycle}, resetting MD")
            self.reset_monthly_md()
            self._current_cycle = cycle

    def update_monthly_md(self, mdi_kva: float) -> None:
        if mdi_kva > self._monthly_md_kva:
            self._monthly_md_kva = mdi_kva

    def reset_monthly_md(self) -> None:
        self._monthly_md_kva = 0.0

    def compute(
        self,
        tick: MeterTick,
        window_state: WindowState,
        anomaly_state: AnomalyState,
        projected_mdi_kva: float,
        billing_cycle_day: int,
    ) -> RiskState:

        contract_kva = self._facility.contract_demand_kva

        # 🔴 FIX: invalid contract demand
        if contract_kva <= 0:
            logger.error("Invalid contract demand (0 or negative)")
            return RiskState(
                timestamp=tick.timestamp,
                window_start=window_state.window_start,
                elapsed_minutes=window_state.elapsed_minutes,
                remaining_minutes=window_state.remaining_minutes,
                accumulated_kVAh=window_state.accumulated_kVAh,
                projected_MDI_kva=projected_mdi_kva,
                contract_demand_kva=contract_kva,
                headroom_kva=0.0,
                risk_level="WARNING",
                months_MD_so_far_kva=self._monthly_md_kva,
                will_set_new_monthly_MD=False,
                billing_cycle_day=billing_cycle_day,
                escalation_reasons=["Invalid contract demand configuration"],
            )

        ratio = projected_mdi_kva / contract_kva

        # 🔴 FIX: INRUSH HANDLING (no forced SAFE/WATCH)
        if anomaly_state.inrush_suppression_active:
            base_level = _base_risk_level(ratio)

            # soften only extreme spike
            if base_level == "CRITICAL":
                risk_level = "WARNING"
            else:
                risk_level = base_level

            return RiskState(
                timestamp=tick.timestamp,
                window_start=window_state.window_start,
                elapsed_minutes=window_state.elapsed_minutes,
                remaining_minutes=window_state.remaining_minutes,
                accumulated_kVAh=window_state.accumulated_kVAh,
                projected_MDI_kva=projected_mdi_kva,
                contract_demand_kva=contract_kva,
                headroom_kva=round(contract_kva - projected_mdi_kva, 2),
                risk_level=risk_level,
                months_MD_so_far_kva=self._monthly_md_kva,
                will_set_new_monthly_MD=False,
                billing_cycle_day=billing_cycle_day,
                escalation_reasons=[
                    "Inrush detected - transient spike suppression applied"
                ],
            )

        # 🔴 FIX: STALE DATA HANDLING (no SAFE, no fake critical branching)
        if anomaly_state.stale_data_detected:
            return RiskState(
                timestamp=tick.timestamp,
                window_start=window_state.window_start,
                elapsed_minutes=window_state.elapsed_minutes,
                remaining_minutes=window_state.remaining_minutes,
                accumulated_kVAh=window_state.accumulated_kVAh,
                projected_MDI_kva=projected_mdi_kva,
                contract_demand_kva=contract_kva,
                headroom_kva=round(contract_kva - projected_mdi_kva, 2),
                risk_level="WARNING",
                months_MD_so_far_kva=self._monthly_md_kva,
                will_set_new_monthly_MD=False,
                billing_cycle_day=billing_cycle_day,
                escalation_reasons=[
                    "Meter data stale - reliability degraded",
                    "System cannot guarantee optimization safety"
                ],
            )

        # --- NORMAL FLOW BELOW ---

        risk_level = _base_risk_level(ratio)
        escalation_reasons: list[str] = []

        # Shift changeover escalation
        if _is_shift_changeover(tick.timestamp, self._facility):
            if risk_level in ("WATCH", "WARNING"):
                escalated = _escalate(risk_level)
                escalation_reasons.append(
                    f"Shift changeover window: {risk_level} → {escalated}"
                )
                risk_level = escalated

        # Billing cycle early days escalation
        if billing_cycle_day <= 3 and risk_level == "WATCH":
            risk_level = "WARNING"
            escalation_reasons.append(
                f"Billing cycle day {billing_cycle_day} ≤ 3: WATCH → WARNING"
            )

        # Load creep escalation
        if anomaly_state.load_creep_detected:
            if risk_level != "CRITICAL":
                escalated = _escalate(risk_level)
                escalation_reasons.append(
                    f"Load creep ({anomaly_state.load_creep_consecutive_windows} consecutive windows): "
                    f"{risk_level} → {escalated}"
                )
                risk_level = escalated

        headroom_kva = contract_kva - projected_mdi_kva
        will_set_new_monthly_md = projected_mdi_kva > self._monthly_md_kva

        logger.debug(
            "[RISK_TRACE] projected=%.1f contract=%.1f ratio=%.3f risk=%s",
            projected_mdi_kva,
            contract_kva,
            projected_mdi_kva / contract_kva,
            risk_level,
        )

        return RiskState(
            timestamp=tick.timestamp,
            window_start=window_state.window_start,
            elapsed_minutes=window_state.elapsed_minutes,
            remaining_minutes=window_state.remaining_minutes,
            accumulated_kVAh=window_state.accumulated_kVAh,
            projected_MDI_kva=projected_mdi_kva,
            contract_demand_kva=contract_kva,
            headroom_kva=round(headroom_kva, 2),
            risk_level=risk_level,
            months_MD_so_far_kva=self._monthly_md_kva,
            will_set_new_monthly_MD=will_set_new_monthly_md,
            billing_cycle_day=billing_cycle_day,
            escalation_reasons=escalation_reasons,
        )