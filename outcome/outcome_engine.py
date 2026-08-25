from __future__ import annotations
from datetime import datetime
import logging

from core.config import TariffConfig, FacilityConfig

logger = logging.getLogger(__name__)


class OutcomeEngine:

    def __init__(self, tariff_config: TariffConfig, facility_config: FacilityConfig):
        self._tariff = tariff_config
        self._facility = facility_config

    def _get_cycle(self, ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts)
            return dt.strftime("%Y-%m")

        except Exception:

            logger.exception(
                "Invalid timestamp for cycle extraction: %s",
                ts,
            )

            raise

    def _load_monthly_state(self, conn, cycle):

        row = conn.execute(
            """
            SELECT actual_peak
            FROM monthly_state
            WHERE facility_id = ?
            AND billing_cycle = ?
            """,
            (
                self._facility.facility_id,
                cycle,
            ),
        ).fetchone()

        if row is None:
            return 0.0

        return row["actual_peak"] or 0.0

    def _update_monthly_state(
        self,
        conn,
        cycle,
        actual_peak
    ):

        conn.execute(
            """
            INSERT INTO monthly_state (
                facility_id,
                billing_cycle,
                actual_peak
            )
            VALUES (?, ?, ?)

            ON CONFLICT(
                facility_id,
                billing_cycle
            )
            DO UPDATE SET
                actual_peak = excluded.actual_peak,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                self._facility.facility_id,
                cycle,
                actual_peak
            )
        )

    def compute(self, conn, window_event: dict) -> dict:

        ts = window_event["window_start"]

        cycle = self._get_cycle(ts)

        actual_mdi = float(
            window_event["actual_mdi"]
        )

        # Previous monthly state
        previous_actual_peak = self._load_monthly_state(conn, cycle)

        # Updated monthly peaks
        actual_peak = max(
            previous_actual_peak,
            actual_mdi
        )

        self._update_monthly_state(
            conn,
            cycle,
            actual_peak
        )

        logger.info(
            f"[OUTCOME_TRACE] "
            f"window={ts} | "
            f"actual={actual_mdi:.2f} | "
            f"monthly_actual_peak={actual_peak:.2f} | "
        )

        return {
            "facility_id": self._facility.facility_id,

            "window_start": ts,

            "actual_mdi": round(actual_mdi, 2),

            "actual_peak": round(actual_peak, 2),

            "window_kvah": round(
                window_event.get(
                    "accumulated_kvah",
                    0.0,
                ),
                2,
            ),
        }