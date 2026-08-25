"""
ingestion/payload_validator.py
PayloadValidator: physical constraint checks on MeterTick.
Rejects on errors, flags warnings. Deterministic.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from core.models import MeterTick, ValidationResult
from core.config import IST, FacilityConfig


NOMINAL_VOLTAGE = 230.0          # Volts, phase-to-neutral
VOLTAGE_ERROR_THRESHOLD = 0.15   # 15% deviation → error
VOLTAGE_WARN_THRESHOLD = 0.10    # 10% deviation → warning
PF_MIN_VALID = 0.70
PF_MAX_VALID = 1.00
PF_PENALTY_THRESHOLD = 0.85
FREQ_MIN = 48.0
FREQ_MAX = 52.0
STALENESS_MAX_SECONDS = 300      # 5 minutes
POWER_TRIANGLE_TOLERANCE = 0.99  # kVA must be >= kW * 0.99


class PayloadValidator:
    """
    Validates each MeterTick against physical constraints.
    Returns ValidationResult with errors (reject) and warnings (accept with flag).
    Maintains monotonic kVAh state per facility.
    """

    def __init__(self, facility_config: FacilityConfig):
        self._facility = facility_config
        self._previous_kvah: Optional[float] = None
        self._previous_timestamp: Optional[datetime] = None

    def validate(self, tick: MeterTick) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        # --- Error checks (reject tick on any error) ---

        # Negative power - no generation in MVP
        if tick.kw < 0:
            errors.append(f"Negative kW: {tick.kw:.3f}. No generation supported in MVP.")
        if tick.kva < 0:
            errors.append(f"Negative kVA: {tick.kva:.3f}. No generation supported in MVP.")
        if tick.kvar < 0:
            errors.append(f"Negative kVAR: {tick.kvar:.3f}. No generation supported in MVP.")

        # PF range
        if not (PF_MIN_VALID <= tick.pf <= PF_MAX_VALID):
            errors.append(
                f"PF {tick.pf:.3f} outside valid range [{PF_MIN_VALID}, {PF_MAX_VALID}]. Physics violation."
            )

        # Frequency range
        if not (FREQ_MIN <= tick.frequency <= FREQ_MAX):
            errors.append(
                f"Frequency {tick.frequency:.2f} Hz outside Indian grid tolerance [{FREQ_MIN}, {FREQ_MAX}]."
            )

        # Power triangle: apparent must be >= real (with tolerance)
        if tick.kva < tick.kw * POWER_TRIANGLE_TOLERANCE:
            errors.append(
                f"Power triangle violation: kVA {tick.kva:.3f} < kW {tick.kw:.3f} × {POWER_TRIANGLE_TOLERANCE}. "
                f"Apparent power cannot be less than real power."
            )

        # kVAh monotonic (no rollover or corruption)
        if self._previous_kvah is not None:
            if tick.kvah_cumulative < self._previous_kvah:
                errors.append(
                    f"kVAh rollback detected: {tick.kvah_cumulative:.3f} < previous {self._previous_kvah:.3f}. "
                    f"Meter rollover or data corruption."
                )

        # Staleness check: reject tick older than 300 seconds from now
        # SIMULATION source is exempt - this check guards against stale MQTT retained messages
        if tick.source != "SIMULATION":
            now_utc = datetime.now(IST)
            tick_ts = tick.timestamp
            if tick_ts.tzinfo is None:
                tick_ts = tick_ts.replace(tzinfo=IST)
            age_seconds = (now_utc - tick_ts).total_seconds()
            if age_seconds > STALENESS_MAX_SECONDS:
                errors.append(
                    f"Stale tick: {age_seconds:.1f}s old. Maximum allowed: {STALENESS_MAX_SECONDS}s. "
                    f"Prevents stale MQTT retain processing."
                )

        # Voltage deviation > 15% from 230V nominal
        for phase, v in [("L1", tick.voltage_l1), ("L2", tick.voltage_l2), ("L3", tick.voltage_l3)]:
            deviation = abs(v - NOMINAL_VOLTAGE) / NOMINAL_VOLTAGE
            if deviation > VOLTAGE_ERROR_THRESHOLD:
                errors.append(
                    f"Voltage {phase} {v:.1f}V deviates {deviation*100:.1f}% from {NOMINAL_VOLTAGE}V nominal. "
                    f"Meter or wiring fault."
                )

        # --- Warning checks (accept with flag) ---

        if not errors:
            # PF below penalty threshold
            if PF_MIN_VALID <= tick.pf < PF_PENALTY_THRESHOLD:
                warnings.append(
                    f"PF {tick.pf:.3f} in penalty range [{PF_MIN_VALID}, {PF_PENALTY_THRESHOLD}). "
                    f"DERC surcharge applies."
                )

            # Voltage deviation 10-15%
            for phase, v in [("L1", tick.voltage_l1), ("L2", tick.voltage_l2), ("L3", tick.voltage_l3)]:
                deviation = abs(v - NOMINAL_VOLTAGE) / NOMINAL_VOLTAGE
                if VOLTAGE_WARN_THRESHOLD <= deviation <= VOLTAGE_ERROR_THRESHOLD:
                    warnings.append(
                        f"Voltage {phase} {v:.1f}V deviation {deviation*100:.1f}% - marginal but accepted."
                    )

            # Load approaching contract limit
            contract_kva = self._facility.contract_demand_kva
            if tick.kva > contract_kva:
                warnings.append(
                    f"kVA {tick.kva:.1f} exceeds contract demand {contract_kva:.1f} kVA. "
                    f"Excess surcharge will apply."
                )
            elif tick.kva > contract_kva * 0.90:
                warnings.append(
                    f"kVA {tick.kva:.1f} is {tick.kva/contract_kva*100:.1f}% of contract demand "
                    f"{contract_kva:.1f} kVA. Approaching limit."
                )

        # Update monotonic state only on valid ticks
        if not errors:
            self._previous_kvah = tick.kvah_cumulative
            self._previous_timestamp = tick.timestamp

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def reset_kvah_state(self) -> None:
        """Call at month boundary when meter resets."""
        self._previous_kvah = None
        self._previous_timestamp = None
