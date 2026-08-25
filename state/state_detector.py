from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

from core.models import MeterTick, WindowState
from core.config import FacilityConfig, SystemConfig

logger = logging.getLogger(__name__)

WINDOW_DURATION_MINUTES = 30


class StateDetector:
    def __init__(
        self,
        facility_config: FacilityConfig,
        system_config: SystemConfig,
        on_window_complete: Optional[Callable[[WindowState, float], Awaitable[None]]] = None,
    ):
        self._facility = facility_config
        self._system = system_config
        self._on_window_complete = on_window_complete

        self._window_start: Optional[datetime] = None
        self._accumulated_kVAh: float = 0.0
        self._last_tick: Optional[MeterTick] = None
        self._completed_window_mdi: float = 0.0

        self._last_kva: Optional[float] = None
        self._window_peak_kva: float = 0.0
        self._pending_tasks: set[asyncio.Task] = set()

    def _compute_window_end(self, window_start: datetime) -> datetime:
        return window_start + timedelta(minutes=WINDOW_DURATION_MINUTES)

    def _elapsed_minutes(self, now: datetime) -> float:
        if self._window_start is None:
            return 0.0
        delta = (now - self._window_start).total_seconds()
        return max(0.0, delta / 60.0)

    def _remaining_minutes(self, now: datetime) -> float:
        return max(0.0, WINDOW_DURATION_MINUTES - self._elapsed_minutes(now))

    def _compute_projected_mdi(self, now: datetime) -> float:
        elapsed_minutes = self._elapsed_minutes(now)

        # Latest observed signal
        latest_kva = self._last_tick.kva if self._last_tick else 0.0

        # Energy-based projection
        elapsed_hours = elapsed_minutes / 60.0
        energy_projection = (
            self._accumulated_kVAh / elapsed_hours
            if elapsed_hours > 0
            else 0.0
        )

        # 🔴 FINAL: unified projection (no time bias)
        return max(
            latest_kva,
            energy_projection
        )

    def _accumulate_kVAh(self, tick: MeterTick) -> None:
        if self._last_tick is None:
            return
        avg_kva = (tick.kva + self._last_tick.kva) / 2.0
        delta_seconds = (tick.timestamp - self._last_tick.timestamp).total_seconds()
        elapsed_hours = delta_seconds / 3600.0
        self._accumulated_kVAh += avg_kva * elapsed_hours

    def _schedule_callback(self, completed_state: WindowState, completed_mdi: float):
        if self._on_window_complete is None:
            return

        async def _runner():
            try:
                await self._on_window_complete(completed_state, completed_mdi)
            except Exception as e:
                logger.error(f"on_window_complete callback failed: {e}")

        task = asyncio.create_task(_runner())
        self._pending_tasks.add(task)
        task.add_done_callback(lambda t: self._pending_tasks.discard(t))

    async def process_tick(self, tick: MeterTick) -> WindowState:
        ts = tick.timestamp

        # 🔴 FIX 1: clamp only for logging, NOT physics
        current_kva = tick.kva

        self._last_kva = current_kva

        # 🔴 FIX 1: use RAW value everywhere
        adjusted_tick = MeterTick(
            timestamp=tick.timestamp,
            facility_id=tick.facility_id,
            kw=tick.kw,
            kva=tick.kva,
            kvar=tick.kvar,
            pf=tick.pf,
            voltage_l1=tick.voltage_l1,
            voltage_l2=tick.voltage_l2,
            voltage_l3=tick.voltage_l3,
            frequency=tick.frequency,
            kvah_cumulative=tick.kvah_cumulative,
            data_quality=tick.data_quality,
            source=tick.source,
            polling_latency_ms=tick.polling_latency_ms,
        )


        # 🔴 INIT WINDOW
        if self._window_start is None:
            self._window_start = ts
            self._accumulated_kVAh = 0.0
            self._window_peak_kva = 0.0
            self._last_tick = adjusted_tick

        # 🔴 also track direct peak
        self._window_peak_kva = max(self._window_peak_kva, tick.kva)

        # 🔴 WINDOW CLOSE
        window_end = self._compute_window_end(self._window_start)

        if ts >= window_end:

            # 🔴 FIX 2: dual MDI model
            completed_mdi = self._window_peak_kva  # control MDI (peak)

            elapsed_hours = WINDOW_DURATION_MINUTES / 60.0
            billing_mdi = self._accumulated_kVAh / elapsed_hours if elapsed_hours > 0 else 0.0

            completed_state = WindowState(
                window_start=self._window_start,
                window_end=window_end,
                elapsed_minutes=WINDOW_DURATION_MINUTES,
                remaining_minutes=0.0,
                accumulated_kVAh=self._accumulated_kVAh,
                last_tick=self._last_tick,
                tick_count=0,
            )

            self._completed_window_mdi = completed_mdi

            logger.info(
                f"Window closed [{self._window_start.strftime('%H:%M:%S')}–"
                f"{window_end.strftime('%H:%M:%S')}] "
                f"PeakMDI={completed_mdi:.1f} kVA | BillingMDI={billing_mdi:.1f} kVA"
            )

            self._schedule_callback(completed_state, completed_mdi)

            # 🔴 RESET WINDOW
            self._window_start = window_end
            self._accumulated_kVAh = 0.0
            self._window_peak_kva = 0.0
            self._last_tick = adjusted_tick

        # 🔴 NORMAL ACCUMULATION
        self._accumulate_kVAh(adjusted_tick)
        self._last_tick = adjusted_tick

        elapsed_min = self._elapsed_minutes(ts)
        remaining_min = self._remaining_minutes(ts)

        return WindowState(
            window_start=self._window_start,
            window_end=self._compute_window_end(self._window_start),
            elapsed_minutes=elapsed_min,
            remaining_minutes=remaining_min,
            accumulated_kVAh=self._accumulated_kVAh,
            last_tick=adjusted_tick,
            tick_count=0,
        )

    @property
    def projected_mdi(self) -> float:
        if self._last_tick is None:
            return 0.0
        return self._compute_projected_mdi(self._last_tick.timestamp)

    @property
    def accumulated_kVAh(self) -> float:
        return self._accumulated_kVAh

    @property
    def window_start(self) -> Optional[datetime]:
        return self._window_start

    @property
    def last_completed_window_mdi(self) -> float:
        return self._completed_window_mdi