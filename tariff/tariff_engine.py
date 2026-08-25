"""
tariff/tariff_engine.py
TOD window classification, effective energy rate computation.
Implements DERC HT Industrial tariff rules exactly.
"""
from __future__ import annotations
import logging
from datetime import datetime

from core.models import MeterTick, TariffState
from core.config import TariffConfig, FacilityConfig

logger = logging.getLogger(__name__)


def _hour_in_ranges(hour: int, ranges: list[list[int]]) -> bool:
    for start, end in ranges:

        # normal range
        if start <= hour < end:
            return True

        # wrapped range (future-safe)
        if start > end:
            if hour >= start or hour < end:
                return True

    return False


def _classify_tod_window(
    ts: datetime,
    tariff: TariffConfig,
) -> tuple[str, float]:

    month = ts.month

    if month not in tariff.tod_applicable_months:
        return "NO_TOD", 1.0

    hour = ts.hour

    if _hour_in_ranges(hour, tariff.peak_hours_summer):
        return "PEAK", tariff.tod_peak_multiplier

    if _hour_in_ranges(hour, tariff.offpeak_hours_summer):
        return "OFF_PEAK", tariff.tod_offpeak_multiplier

    return "NORMAL", 1.0


class TariffEngine:
    """
    Computes TOD window and effective energy rate for each tick.
    Voltage rebate applies to energy charges only.
    TOD mandatory for all consumers with sanctioned load >= 11 kVA.
    No TOD October-April.
    """

    def __init__(self, tariff_config: TariffConfig, facility_config: FacilityConfig):
        self._tariff = tariff_config
        self._facility = facility_config
        self._voltage_level = facility_config.voltage_level

    def compute(self, tick: MeterTick) -> TariffState:
        ts = tick.timestamp
        month = ts.month
        is_tod_applicable = (
            month in self._tariff.tod_applicable_months
        )

        tod_window, tod_multiplier = _classify_tod_window(
            ts,
            self._tariff,
        )

        voltage_rebate = self._tariff.voltage_rebate(self._voltage_level)
        # rebate factor: 1 - rebate (e.g., 11kV → 0.97)
        voltage_rebate_factor = 1.0 - voltage_rebate

        base_rate = self._tariff.energy_charge_per_kVAh  # 7.75 ₹/kVAh

        # After voltage rebate
        rebated_rate = base_rate * voltage_rebate_factor  # = 7.52 for 11kV

        # Apply TOD multiplier
        effective_rate = rebated_rate * tod_multiplier

        logger.debug(
            f"TariffEngine: month={month}, TOD={tod_window}, multiplier={tod_multiplier}, "
            f"rate=₹{effective_rate:.4f}/kVAh"
        )

        return TariffState(
            timestamp=ts,
            tod_window=tod_window,
            effective_energy_rate=round(effective_rate, 5),
            base_energy_rate=base_rate,
            tod_multiplier=tod_multiplier,
            voltage_rebate_factor=voltage_rebate_factor,
            is_tod_applicable=is_tod_applicable,
            month=month,
        )