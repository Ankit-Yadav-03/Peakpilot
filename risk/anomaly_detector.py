from __future__ import annotations
import logging
from collections import deque
from datetime import datetime
from typing import Optional, Deque

from core.models import MeterTick, AnomalyState
from core.config import FacilityConfig, SystemConfig

logger = logging.getLogger(__name__)

INRUSH_SPIKE_THRESHOLD_PCT = 0.45   # 🔴 increased sensitivity for real spikes
INRUSH_MIN_SAMPLES = 5              # 🔴 faster detection
STALE_CONFIDENCE_CAP = 0.3


class AnomalyDetector:

    def __init__(self, facility_config: FacilityConfig, system_config: SystemConfig):
        self._facility = facility_config
        self._system = system_config

        # Inrush state
        self._inrush_active = False
        self._inrush_start_ts = None
        self._last_kva = None
        self._inrush_detected_at: Optional[datetime] = None
        self._inrush_window_start: Optional[datetime] = None
        self._window_avg_kva: float = 0.0
        self._window_kva_samples: Deque[float] = deque(maxlen=60)
        self._last_inrush_end: Optional[datetime] = None
        self._inrush_cooldown_seconds: int = 10

        # Load creep
        self._window_mdi_history: Deque[float] = deque(maxlen=10)
        self._load_creep_count: int = 0
        self._load_creep_detected: bool = False

        # Stale data
        self._last_good_tick_time: Optional[datetime] = None
        self._polling_interval: float = float(system_config.polling_interval_seconds)
        self._stale_threshold_seconds: float = float(system_config.stale_data_threshold_seconds)

    def on_window_complete(self, window_projected_mdi: float) -> None:
        # 🔴 DO NOT reset inrush here (fix)
        self._window_kva_samples.clear()

        threshold = self._system.load_creep_threshold_pct / 100.0
        if self._window_mdi_history:
            prev = self._window_mdi_history[-1]
            if prev > 0 and window_projected_mdi >= prev * (1 + threshold):
                self._load_creep_count += 1
                logger.debug(f"Load creep tick: count={self._load_creep_count}")
            else:
                self._load_creep_count = 0

        self._load_creep_detected = (
            self._load_creep_count >= self._system.load_creep_consecutive_windows
        )
        self._window_mdi_history.append(window_projected_mdi)

    def detect(self, tick: MeterTick, window_start: Optional[datetime]) -> AnomalyState:
        flags: list[str] = []

        inrush_detected = False
        inrush_suppression_active = False

        # --- Track last GOOD tick ---
        if tick.data_quality == "GOOD":
            self._last_good_tick_time = tick.timestamp

        # --- INRUSH DETECTION (NEW LOGIC) ---
        current_kva = tick.kva

        if self._last_kva is not None:
            delta_pct = (current_kva - self._last_kva) / max(self._last_kva, 1)

            # 🔴 detect sudden spike
            cooldown_active = (
                self._last_inrush_end is not None and
                (tick.timestamp - self._last_inrush_end).total_seconds() < self._inrush_cooldown_seconds
            )

            is_large_relative_jump = delta_pct > INRUSH_SPIKE_THRESHOLD_PCT
            is_high_absolute_load = current_kva > (self._facility.contract_demand_kva * 0.85)

            if is_large_relative_jump and is_high_absolute_load and not cooldown_active:
                self._inrush_active = True
                self._inrush_start_ts = tick.timestamp
                self._inrush_detected_at = tick.timestamp

                inrush_detected = True

                flags.append(
                    f"INRUSH: kVA jump {self._last_kva:.1f} → {current_kva:.1f} "
                    f"({delta_pct*100:.1f}% spike)"
                )

                logger.warning(f"Inrush detected at {tick.timestamp}: {flags[-1]}")

        self._last_kva = current_kva

        # --- INRUSH SUPPRESSION WINDOW ---
        if self._inrush_active:
            duration = (tick.timestamp - self._inrush_start_ts).total_seconds()

            if duration <= self._system.inrush_window_seconds:
                inrush_suppression_active = True
                flags.append(f"INRUSH_SUPPRESSION_ACTIVE ({duration:.0f}s)")
            else:
                # 🔴 auto-expire
                self._inrush_active = False
                self._last_inrush_end = tick.timestamp
                self._inrush_start_ts = None

        # --- LOAD CREEP ---
        if self._load_creep_detected:
            flags.append(
                f"LOAD_CREEP: {self._load_creep_count} consecutive windows each "
                f">={self._system.load_creep_threshold_pct}% above previous"
            )

        # --- STALE DATA ---
        stale_detected = False
        confidence_cap: Optional[float] = None

        if tick.data_quality == "STALE":
            stale_detected = True
            confidence_cap = STALE_CONFIDENCE_CAP
            flags.append(f"STALE_DATA: data_quality=STALE")
        elif self._last_good_tick_time is not None:
            elapsed = (tick.timestamp - self._last_good_tick_time).total_seconds()

            if elapsed > self._stale_threshold_seconds:
                stale_detected = True
                confidence_cap = STALE_CONFIDENCE_CAP
                flags.append(
                    f"STALE_DATA: {elapsed:.0f}s since last GOOD tick"
                )

        return AnomalyState(
            timestamp=tick.timestamp,
            inrush_detected=inrush_detected,
            inrush_suppression_active=inrush_suppression_active,
            load_creep_detected=self._load_creep_detected,
            load_creep_consecutive_windows=self._load_creep_count,
            stale_data_detected=stale_detected,
            confidence_cap=confidence_cap,
            anomaly_flags=flags,
        )