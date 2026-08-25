from __future__ import annotations

from datetime import datetime
import time
import sqlite3
import logging
import uuid

from learning.event_logger import DurableEventLogger
from outcome.outcome_engine import OutcomeEngine

logger = logging.getLogger(__name__)


class OutcomeWorker:

    """
    Reconciles recommendation decisions with telemetry-confirmed executions.

    IMPORTANT:
    This worker DOES NOT compute realized economics.

    It only tracks:
    - recommendation issuance
    - telemetry execution state
    - projected economic metadata
    - reconciliation lifecycle

    Realized causal savings attribution is intentionally deferred
    until sufficient telemetry history exists.
    """

    def __init__(
        self,
        db_path,
        outcome_engine,
        event_logger,
        confidence_engine,
    ):
        self._db_path = db_path

        self._engine: OutcomeEngine = outcome_engine

        self._event_logger: DurableEventLogger = event_logger
        self._confidence_engine = confidence_engine 

    def _get_connection(self):

        conn = sqlite3.connect(
            self._db_path,
            timeout=30
        )

        conn.row_factory = sqlite3.Row

        conn.execute(
            "PRAGMA busy_timeout = 30000;"
        )

        conn.execute(
            "PRAGMA journal_mode=WAL;"
        )

        return conn

    def run(self):

        logger.info(
            "OutcomeWorker started"
        )

        while True:

            conn = None

            try:

                conn = self._get_connection()

                cursor = conn.cursor()

                # ---------------------------------------------------------
                # Fetch decisions without reconciliation outcome
                # ---------------------------------------------------------

                cursor.execute("""
                    SELECT
                        d.decision_id,
                        d.facility_id,
                        d.projected_mdi_kva,
                        d.expected_mdi_reduction_kva,
                        d.projected_saving_rupees,
                        d.economic_status,
                        d.saving_basis
                    FROM decision_events d
                    WHERE d.decision_id NOT IN (
                        SELECT decision_id
                        FROM outcome_events
                    )
                    LIMIT 20
                """)

                decisions = cursor.fetchall()

                for d in decisions:

                    decision_id = d["decision_id"]

                    # -----------------------------------------------------
                    # Telemetry execution lookup
                    # -----------------------------------------------------

                    executions_cursor = cursor.execute("""
                        SELECT
                            status,
                            confirmation_source,
                            confirmed_at,
                            measured_delta_kva,
                            confirmation_latency_ms
                        FROM execution_events
                        WHERE decision_id = ?
                    """, (decision_id,))

                    executions = executions_cursor.fetchall()

                    if not executions:
                        continue

                    total_loads = len(executions)

                    followed_loads = sum(
                        1
                        for row in executions
                        if row["status"] in (
                            "SHED_CONFIRMED",
                            "RESTORE_CONFIRMED",
                        )
                        and row["confirmation_source"] in (
                            "EQUIPMENT",
                            "EQUIPMENT_AND_METER",
                        )
                    )

                    ignored_loads = total_loads - followed_loads

                    compliance_pct = round(
                        (
                            followed_loads / total_loads
                        ) * 100.0,
                        2
                    )

                    statuses = { row["status"] for row in executions } 

                    terminal_statuses = { 
                        "SHED_CONFIRMED", 
                        "EXECUTION_NOT_CONFIRMED", 
                        "RESTORE_CONFIRMED", 
                        "RESTORE_NOT_CONFIRMED", 
                    }
                    
                    if not statuses.issubset(terminal_statuses): 
                        continue

                    latest_execution = max(
                        executions,
                        key=lambda row: (
                            datetime.fromisoformat(
                                row["confirmed_at"]
                            )
                            if row["confirmed_at"]
                            else datetime.min
                        )
                    )

                    confirmation_source = latest_execution["confirmation_source"]

                    if compliance_pct == 100:
                        action_status = "FOLLOWED"

                    elif compliance_pct == 0:
                        action_status = "IGNORED"

                    else:
                        action_status = "PARTIAL_CONFIRMATION"

                    confirmed_at = latest_execution["confirmed_at"]

                    delta_values = [
                        row["measured_delta_kva"]
                        for row in executions
                        if row["measured_delta_kva"] is not None
                    ]

                    measured_delta_kva = (
                        sum(delta_values)
                        if delta_values
                        else None
                    )
                    
                    latencies = [
                        row["confirmation_latency_ms"]
                        for row in executions
                        if row["confirmation_latency_ms"] is not None
                    ]

                    confirmation_latency_ms = (
                        max(latencies)
                        if latencies
                        else None
                    )

                    existing = cursor.execute("""
                        SELECT 1
                        FROM outcome_events
                        WHERE decision_id = ?
                        LIMIT 1
                    """, (decision_id,)).fetchone()

                    if existing is not None:
                        continue

                    # -----------------------------------------------------
                    # Reconciliation payload
                    # -----------------------------------------------------

                    payload = {

                        "event_id": str(uuid.uuid4()),

                        "decision_id": decision_id,

                        "action_status": action_status,
                        "confirmation_source": confirmation_source,
                        "confirmed_at": confirmed_at,
                        "measured_delta_kva": measured_delta_kva,
                        "confirmation_latency_ms": confirmation_latency_ms,

                        "total_loads": total_loads,

                        "followed_loads": followed_loads,

                        "ignored_loads": ignored_loads,

                        "compliance_pct": compliance_pct,

                        "facility_id": d["facility_id"],

                        # projected economics only
                        "projected_saving": (
                            d["projected_saving_rupees"]
                        ),

                        "economic_status": (
                            d["economic_status"]
                        ),

                        "saving_basis": (
                            d["saving_basis"]
                        ),
                    }

                    # -----------------------------------------------------
                    # WAL write
                    # -----------------------------------------------------    
                    self._event_logger.log_outcome_event(payload)

                    logger.info(
                        "[OUTCOME_RECONCILED] "
                        "decision=%s | "
                        "status=%s | "
                        "projected_saving=%s",
                        decision_id,
                        action_status,
                        d["projected_saving_rupees"],
                    )

                cursor.execute("""
                    SELECT *
                    FROM window_events
                    WHERE processed = 0
                    ORDER BY id
                    LIMIT 20
                """)

                windows = cursor.fetchall()

                for row in windows:

                    try:

                        outcome = self._engine.compute(
                            conn,
                            dict(row),
                        )

                        conn.commit()

                        self._event_logger.log_window_outcome(
                            outcome
                        )

                        self._event_logger.mark_window_processed(
                            row["id"]
                        )

                        conn.commit()

                        logger.info(
                            "[WINDOW_OUTCOME] "
                            "window=%s ",
                            row["window_start"]
                        )

                    except Exception:

                        logger.exception(
                            "[WINDOW_OUTCOME_FAILED] id=%s",
                            row["id"],
                        )

                time.sleep(2)

            except Exception as e:

                logger.error(
                    "OutcomeWorker error: %s",
                    e
                )

                time.sleep(2)

            finally:
                if conn is not None:
                    conn.close()
