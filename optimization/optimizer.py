"""
optimization/optimizer.py
TODOptimizer: identify load-shift and pre-cooling opportunities.
Pre-cooling applicable when within 120 min of TOD peak.
Disabled during shift changeover windows.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Dict, Optional

from core.models import MeterTick, TariffState, RiskState, TODRecommendation
from core.config import FacilityConfig, TariffConfig

logger = logging.getLogger(__name__)

PRE_COOLING_WINDOW_MINUTES = 120


def _minutes_to_peak(ts: datetime, tariff_state: TariffState) -> Optional[float]:
    """
    Returns minutes until next TOD peak window starts.
    Returns None if not applicable or already in peak.
    """
    if not tariff_state.is_tod_applicable:
        return None
    if tariff_state.tod_window == "PEAK":
        return None

    hour = ts.hour
    minute = ts.minute
    current_minutes_of_day = hour * 60 + minute

    peak_starts = [14 * 60, 22 * 60]  # 14:00, 22:00
    closest = None
    for start in peak_starts:
        diff = start - current_minutes_of_day
        if diff < 0:
            diff += 24 * 60  # Next day
        if closest is None or diff < closest:
            closest = diff
    return closest


def _is_shift_changeover(ts: datetime, facility_config: FacilityConfig) -> bool:
    tod_minutes = ts.hour * 60 + ts.minute
    ss = facility_config.shift_schedule
    for shift_str in [ss.shift_1_start, ss.shift_2_start]:
        h, m = shift_str.split(":")
        start_min = int(h) * 60 + int(m)
        if abs(tod_minutes - start_min) <= 15:
            return True
    return False


class TODOptimizer:
    """
    Generates TOD-based load shift and pre-cooling recommendations.
    Disabled during shift changeover windows (spec: disable TOD optimizer in changeover window).
    """

    def __init__(self, facility_config: FacilityConfig, tariff_config: TariffConfig):
        self._facility = facility_config
        self._tariff = tariff_config
        self._loads_raw: List[Dict] = facility_config.loads_raw.get("loads", [])
        self._last_logged_signature = None

    def _get_shiftable_loads(self) -> List[Dict]:
        return [l for l in self._loads_raw if l.get("shiftable") and l.get("priority", 1) != 1]
    
    def _log_if_changed(
        self,
        action: str,
        loads: list[str],
        saving: float,
    ) -> None:

        signature = (
            action,
            tuple(sorted(loads)),
        )

        if signature == self._last_logged_signature:
            return

        self._last_logged_signature = signature

        logger.info(
            "TODOptimizer: %s loads=%s saving=₹%.0f",
            action,
            loads,
            saving,
        )

    def compute(
        self,
        tick: MeterTick,
        tariff_state: TariffState,
        risk_state: RiskState,
    ) -> TODRecommendation:
        ts = tick.timestamp

        # Disabled during shift changeover
        if _is_shift_changeover(ts, self._facility):
            return TODRecommendation(
                timestamp=ts,
                action="NO_ACTION",
                loads=[],
                estimated_saving_rupees=0.0,
                rationale="TOD optimizer disabled during shift changeover window.",
                pre_cooling_applicable=False,
            )

        if not tariff_state.is_tod_applicable:
            return TODRecommendation(
                timestamp=ts,
                action="NO_ACTION",
                loads=[],
                estimated_saving_rupees=0.0,
                rationale="TOD not applicable this month (October-April).",
                pre_cooling_applicable=False,
            )

        minutes_to_peak = _minutes_to_peak(ts, tariff_state)
        pre_cooling_applicable = False
        action = "NO_ACTION"
        candidate_loads = []
        estimated_saving = 0.0
        rationale = ""

        current_window = tariff_state.tod_window

        if current_window == "OFF_PEAK":
            # Pre-cooling opportunity: run thermal-mass loads now, before peak
            shiftable = self._get_shiftable_loads()
            thermal_loads = [
                l for l in shiftable
                if l.get("thermal_time_constant_minutes", 0) > 0
                and l.get("max_delay_minutes", 0) >= 15
            ]

            if thermal_loads:
                pre_cooling_applicable = True
                action = "PRE_RUN"
                candidate_loads = [l["load_id"] for l in thermal_loads]
                # Saving: running at off-peak rate vs peak rate per kVAh
                total_kva = sum(
                    l["typical_kw"] / max(l["typical_pf"], 0.01) for l in thermal_loads
                )
                # Conservative tariff opportunity estimate only.
                # NOT guaranteed economic saving.

                rebate = (
                    1.0
                    - self._tariff.voltage_rebate(
                        self._facility.voltage_level
                    )
                )

                base = (
                    self._tariff.energy_charge_per_kVAh
                    * rebate
                )

                tariff_delta = (
                    self._tariff.tod_peak_multiplier
                    - self._tariff.tod_offpeak_multiplier
                )

                # Conservative assumption:
                # only 15 minutes of thermal displacement opportunity
                estimated_shiftable_kvah = (
                    total_kva * 0.25
                )

                estimated_saving = (
                    estimated_shiftable_kvah
                    * base
                    * tariff_delta
                )

                rationale = (
                    f"Tariff-aware pre-run opportunity detected. "
                    f"{len(thermal_loads)} thermal loads may shift part of their "
                    f"energy usage away from upcoming peak pricing. "
                    f"{minutes_to_peak:.0f} min to peak start."
                )

                self._log_if_changed(
                    action="PRE_RUN",
                    loads=candidate_loads,
                    saving=estimated_saving,
                )

        elif current_window == "PEAK":
            # Shift schedulable loads away from peak
            shiftable = self._get_shiftable_loads()
            peak_shiftable = [
                l for l in shiftable
                if l.get("max_delay_minutes", 0) >= 30
            ]
            if peak_shiftable:
                action = "SHIFT"
                candidate_loads = [l["load_id"] for l in peak_shiftable]
                total_kva = sum(
                    l["typical_kw"] / max(l["typical_pf"], 0.01) for l in peak_shiftable
                )
                rebate = 1.0 - self._tariff.voltage_rebate(self._facility.voltage_level)
                base = self._tariff.energy_charge_per_kVAh * rebate
                # Conservative tariff opportunity estimate only.
                # NOT guaranteed saving realization.

                tariff_delta = (
                    self._tariff.tod_peak_multiplier
                    - 1.0
                )

                # Conservative assumption:
                # only partial operational shifting is achievable
                estimated_shiftable_kvah = (
                    total_kva * 1.0
                )

                estimated_saving = (
                    estimated_shiftable_kvah
                    * base
                    * tariff_delta
                )

                rationale = (
                    f"TOD peak pricing window active. "
                    f"Operational load shifting may reduce exposure "
                    f"to peak energy tariffs for {len(peak_shiftable)} loads."
                )

                self._log_if_changed(
                    action="SHIFT",
                    loads=candidate_loads,
                    saving=estimated_saving,
                )

        elif minutes_to_peak is not None and minutes_to_peak <= PRE_COOLING_WINDOW_MINUTES:
            # Approaching peak: recommend pre-cooling / pre-run
            shiftable = self._get_shiftable_loads()
            thermal_loads = [
                l for l in shiftable
                if l.get("thermal_time_constant_minutes", 0) > 0
            ]
            if thermal_loads:
                pre_cooling_applicable = True
                action = "PRE_RUN"
                candidate_loads = [l["load_id"] for l in thermal_loads]
                rationale = (
                    f"{minutes_to_peak:.0f} min to TOD peak. Pre-run thermal loads now to "
                    f"reduce peak consumption."
                )

        if not candidate_loads:
            action = "NO_ACTION"
            rationale = rationale or f"No actionable TOD opportunities. Window: {current_window}."

        if action == "NO_ACTION":
            self._last_logged_signature = None

        return TODRecommendation(
            timestamp=ts,
            action=action,
            loads=candidate_loads,
            estimated_saving_rupees=round(estimated_saving, 2),
            rationale=rationale,
            pre_cooling_applicable=pre_cooling_applicable,
        )
