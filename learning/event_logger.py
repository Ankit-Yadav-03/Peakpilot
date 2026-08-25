"""
learning/event_logger.py
DurableEventLogger: two-phase WAL write + async background flush.
Phase 1 (sync): write to event_wal table, < 1ms. Pipeline never blocks.
Phase 2 (async): background flush worker writes to destination table, marks flushed=1.
Crash recovery: replay_wal() on startup before starting flush worker.
"""
from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.config import IST
from core.models import MeterTick, Recommendation, RiskState, WindowState, TariffState

logger = logging.getLogger(__name__)

WAL_BATCH_SIZE = 50
WAL_FLUSH_INTERVAL_SECONDS = 2.0


class DurableEventLogger:
    """
    Thread-safe WAL-based event logger.
    All writes go through the two-phase WAL protocol.
    """

    def __init__(self, db_path: str, schema_path: str):
        self._db_path = db_path
        self._schema_path = schema_path
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_failed_count = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        schema = Path(self._schema_path).read_text()
        conn = self._get_conn()
        try:
            conn.executescript(schema)
            self._migrate_decision_events_checks(conn)
            self._migrate_outcome_events_columns(conn)
            conn.commit()
            logger.info(f"Database initialized: {self._db_path}")
        finally:
            conn.close()

    def _migrate_decision_events_checks(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'decision_events'"
        ).fetchone()
        create_sql = row[0] if row else ""
        if "RESTORE" in create_sql and "SYSTEM" in create_sql:
            return

        logger.warning("Migrating decision_events CHECK constraints for Phase A.")
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("ALTER TABLE decision_events RENAME TO decision_events_old")
        conn.execute("""
            CREATE TABLE decision_events (
                event_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                facility_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                trigger TEXT NOT NULL CHECK(trigger IN ('DEMAND_RISK','TOD_OPTIMIZATION','SCHEDULED','SYSTEM')),
                risk_level TEXT NOT NULL CHECK(risk_level IN ('SAFE','WATCH','WARNING','CRITICAL')),
                projected_mdi_kva REAL NOT NULL,
                contract_demand_kva REAL NOT NULL,
                headroom_kva REAL NOT NULL,
                remaining_window_minutes REAL NOT NULL,
                tod_window TEXT NOT NULL,
                billing_cycle_day INTEGER NOT NULL,
                recommendation_type TEXT NOT NULL CHECK(recommendation_type IN ('SHED','DELAY','PRE_RUN','NO_ACTION','RESTORE')),
                loads_selected TEXT NOT NULL,
                expected_mdi_reduction_kva REAL NOT NULL,
                prevented_md_kva REAL,
                projected_saving_rupees REAL,
                economic_status TEXT,
                saving_basis TEXT,
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
                condition_familiarity_score REAL,
                intelligent_layer_override INTEGER NOT NULL DEFAULT 0,
                override_reason TEXT,
                conflict_resolved INTEGER NOT NULL DEFAULT 0,
                conflict_resolution TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO decision_events (
                event_id, decision_id, facility_id, timestamp, trigger,
                risk_level, projected_mdi_kva, contract_demand_kva, headroom_kva,
                remaining_window_minutes, tod_window, billing_cycle_day,
                recommendation_type, loads_selected, expected_mdi_reduction_kva,
                prevented_md_kva, projected_saving_rupees, economic_status,
                saving_basis, confidence, condition_familiarity_score,
                intelligent_layer_override, override_reason, conflict_resolved,
                conflict_resolution
            )
            SELECT
                event_id, decision_id, facility_id, timestamp, trigger,
                risk_level, projected_mdi_kva, contract_demand_kva, headroom_kva,
                remaining_window_minutes, tod_window, billing_cycle_day,
                recommendation_type, loads_selected, expected_mdi_reduction_kva,
                prevented_md_kva, projected_saving_rupees, economic_status,
                saving_basis, confidence, condition_familiarity_score,
                intelligent_layer_override, override_reason, conflict_resolved,
                conflict_resolution
            FROM decision_events_old
        """)
        conn.execute("DROP TABLE decision_events_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_facility_time ON decision_events(facility_id, timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_risk ON decision_events(facility_id, risk_level, billing_cycle_day)")
        conn.execute("PRAGMA foreign_keys = ON;")

    def _migrate_outcome_events_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(outcome_events)").fetchall()
        }
        additions = {
            "confirmation_source": "TEXT",
            "confirmed_at": "TEXT",
            "measured_delta_kva": "REAL",
            "confirmation_latency_ms": "REAL",
        }
        for column, column_type in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE outcome_events ADD COLUMN {column} {column_type}")

    # -----------------------------------------------------------------------
    # Phase 1: Synchronous WAL write (< 1ms)
    # -----------------------------------------------------------------------

    def _wal_write(self, table_name: str, payload: Dict[str, Any]) -> int:
        """Write record to event_wal. Returns wal_id."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO event_wal (table_name, payload, written_at) VALUES (?, ?, ?)",
                (table_name, json.dumps(payload, default=str), datetime.now(IST).isoformat()),
            )
            conn.commit()
            wal_id = cursor.lastrowid
            return wal_id
        except sqlite3.Error:
            logger.exception(
                "WAL write failed for table=%s",
                table_name,
            )
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Phase 2: Async flush worker
    # -----------------------------------------------------------------------

    async def start_flush_worker(self) -> None:
        """Start background WAL flush task."""
        if self._flush_task is not None:
            if not self._flush_task.done():
                logger.warning(
                    "WAL flush worker already running."
                )
                return
            
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("WAL flush worker started.")

    async def stop_flush_worker(self) -> None:
        """Gracefully stop flush worker."""
        self._running = False
        await asyncio.get_event_loop().run_in_executor(None, self._flush_pending)
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        logger.info("WAL flush worker stopped.")

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._flush_pending)
            except Exception as e:
                logger.error(f"WAL flush error: {e}")
            await asyncio.sleep(WAL_FLUSH_INTERVAL_SECONDS)

    def _flush_pending(self) -> None:
        """Flush all unflushed WAL records to destination tables."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """
                SELECT wal_id, table_name, payload
                FROM event_wal
                WHERE flushed = 0
                ORDER BY wal_id ASC
                LIMIT ?
                """,
                (WAL_BATCH_SIZE,)
            ).fetchall()

            failed_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM event_wal
                WHERE flushed = -1
                """
            ).fetchone()[0]

            if failed_count > 0:

                if failed_count != self._last_failed_count:

                    logger.error(
                        "WAL contains %s permanently failed records.",
                        failed_count,
                    )

                    self._last_failed_count = failed_count

            elif self._last_failed_count is not None:

                logger.info(
                    "WAL permanent failure queue cleared."
                )

                self._last_failed_count = None

            for row in rows:
                wal_id = row[0]
                table_name = row[1]
                payload = json.loads(row[2])
                try:
                    self._insert_to_table(conn, table_name, payload)
                    conn.execute(
                        "UPDATE event_wal SET flushed = 1 WHERE wal_id = ?", (wal_id,)
                    )
                except Exception:
                    logger.exception(
                        "WAL flush failed | wal_id=%s",
                        wal_id,
                    )

                    conn.execute(
                        """
                        UPDATE event_wal
                        SET flushed = -1
                        WHERE wal_id = ?
                        """,
                        (wal_id,)
                    )

            conn.execute(
                """
                DELETE FROM event_wal
                WHERE flushed = 1
                AND wal_id < (
                    SELECT MAX(wal_id)
                    FROM event_wal
                ) - 10000
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_to_table(self, conn: sqlite3.Connection, table_name: str, payload: Dict) -> None:
        if table_name == "telemetry_events":
            conn.execute("""
                INSERT OR IGNORE INTO telemetry_events (
                    event_id, facility_id, timestamp, kw, kva, kvar,
                    kvah_cumulative, pf, voltage_l1, voltage_l2, voltage_l3,
                    frequency, window_start, accumulated_kvah_this_window,
                    projected_mdi_kva, tod_window, source, polling_latency_ms, data_quality
                ) VALUES (
                    :event_id, :facility_id, :timestamp, :kw, :kva, :kvar,
                    :kvah_cumulative, :pf, :voltage_l1, :voltage_l2, :voltage_l3,
                    :frequency, :window_start, :accumulated_kvah_this_window,
                    :projected_mdi_kva, :tod_window, :source, :polling_latency_ms, :data_quality
                )
            """, payload)

        elif table_name == "decision_events":
            conn.execute("""
                INSERT OR IGNORE INTO decision_events (
                    event_id, decision_id, facility_id, timestamp, trigger,
                    risk_level, projected_mdi_kva, contract_demand_kva, headroom_kva,
                    remaining_window_minutes, tod_window, billing_cycle_day,
                    recommendation_type, loads_selected, expected_mdi_reduction_kva,
                    prevented_md_kva,
                    projected_saving_rupees,
                    economic_status,
                    saving_basis,     
                    confidence, condition_familiarity_score,
                    intelligent_layer_override, override_reason, conflict_resolved, conflict_resolution
                ) VALUES (
                    :event_id, :decision_id, :facility_id, :timestamp, :trigger,
                    :risk_level, :projected_mdi_kva, :contract_demand_kva, :headroom_kva,
                    :remaining_window_minutes, :tod_window, :billing_cycle_day,
                    :recommendation_type, :loads_selected, :expected_mdi_reduction_kva,
                    :prevented_md_kva, :projected_saving_rupees, :economic_status, :saving_basis,
                    :confidence, :condition_familiarity_score,
                    :intelligent_layer_override, :override_reason, :conflict_resolved, :conflict_resolution
                )
            """, payload)

        elif table_name == "execution_events":
            conn.execute("""
                INSERT INTO execution_events (
                    event_id, decision_id, facility_id, load_id, command_type,
                    expected_state, status, issued_at, updated_at, confirmed_at,
                    confirmation_latency_ms, confirmation_source,
                    pre_equipment_running, post_equipment_running,
                    equipment_last_update, equipment_quality,
                    pre_kva, post_kva, expected_delta_kva, measured_delta_kva,
                    telemetry_quality, failure_reason, metadata
                ) VALUES (
                    :event_id, :decision_id, :facility_id, :load_id, :command_type,
                    :expected_state, :status, :issued_at, :updated_at, :confirmed_at,
                    :confirmation_latency_ms, :confirmation_source,
                    :pre_equipment_running, :post_equipment_running,
                    :equipment_last_update, :equipment_quality,
                    :pre_kva, :post_kva, :expected_delta_kva, :measured_delta_kva,
                    :telemetry_quality, :failure_reason, :metadata
                ) ON CONFLICT(event_id)
                DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    confirmed_at = excluded.confirmed_at,
                    confirmation_latency_ms =
                        excluded.confirmation_latency_ms,
                    confirmation_source =
                        excluded.confirmation_source,
                    post_equipment_running =
                        excluded.post_equipment_running,
                    equipment_last_update =
                        excluded.equipment_last_update,
                    equipment_quality =
                        excluded.equipment_quality,
                    post_kva =
                        excluded.post_kva,
                    measured_delta_kva =
                        excluded.measured_delta_kva,
                    telemetry_quality =
                        excluded.telemetry_quality,
                    failure_reason =
                        excluded.failure_reason,
                    metadata =
                        excluded.metadata       
            """, payload)

        elif table_name == "outcome_events":

            payload.setdefault("action_status", "TELEMETRY_OUTCOME")
            payload.setdefault("confirmation_source", None)
            payload.setdefault("confirmed_at", None)
            payload.setdefault("measured_delta_kva", None)
            payload.setdefault("confirmation_latency_ms", None)
            payload.setdefault("total_loads", None)
            payload.setdefault("followed_loads", None)
            payload.setdefault("ignored_loads", None)
            payload.setdefault("compliance_pct", None)
            payload.setdefault("projected_saving", payload.get("actual_saving_rupees"))
            payload.setdefault("economic_status", None)
            payload.setdefault("saving_basis", None)

            conn.execute("""
                INSERT OR REPLACE INTO outcome_events (
                    event_id,
                    decision_id,
                    facility_id,
                    action_status,
                    confirmation_source,
                    confirmed_at,
                    measured_delta_kva,
                    confirmation_latency_ms,
                    total_loads,
                    followed_loads,
                    ignored_loads,
                    compliance_pct,
                    projected_saving,
                    economic_status,
                    saving_basis
                ) VALUES (
                    :event_id,
                    :decision_id,
                    :facility_id,
                    :action_status,
                    :confirmation_source,
                    :confirmed_at,
                    :measured_delta_kva,
                    :confirmation_latency_ms,
                    :total_loads,
                    :followed_loads,
                    :ignored_loads,
                    :compliance_pct,
                    :projected_saving,
                    :economic_status,
                    :saving_basis
                )
            """, payload)

        elif table_name == "window_outcomes":
            conn.execute("""
                INSERT INTO window_outcomes (
                    facility_id,
                    window_start,
                    actual_mdi,
                    actual_peak,
                    window_kvah
                )
                VALUES (
                    :facility_id,
                    :window_start,
                    :actual_mdi,
                    :actual_peak,
                    :window_kvah
                )
            """, payload)

        elif table_name == "window_events":
            conn.execute("""
                INSERT INTO window_events (
                    window_start,
                    window_end,
                    actual_mdi,
                    accumulated_kvah,
                    processed
                ) VALUES (
                    :window_start,
                    :window_end,
                    :actual_mdi,
                    :accumulated_kvah,
                    :processed
                )
            """, payload)

        elif table_name == "window_events_processed":
            conn.execute("""
                UPDATE window_events
                SET processed = 1
                WHERE id = :id
            """, payload)

        else:
            raise ValueError(f"Unknown table: {table_name}")

    # -----------------------------------------------------------------------
    # Crash recovery
    # -----------------------------------------------------------------------

    def replay_wal(self) -> List[int]:

        conn = self._get_conn()

        try:

            rows = conn.execute(
                "SELECT wal_id FROM event_wal "
                "WHERE flushed = 0 "
                "ORDER BY wal_id ASC"
            ).fetchall()

            unflushed_ids = [r[0] for r in rows]

        finally:
            conn.close()

        if unflushed_ids:

            logger.warning(
                "WAL replay: %s unflushed records found. Replaying.",
                len(unflushed_ids),
            )

            while True:

                self._flush_pending()

                verify_conn = self._get_conn()

                try:

                    remaining = verify_conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM event_wal
                        WHERE flushed = 0
                        """
                    ).fetchone()[0]

                finally:

                    verify_conn.close()

                if remaining == 0:
                    break

            logger.info(
                "WAL replay successful."
            )

    # -----------------------------------------------------------------------
    # Public write methods
    # -----------------------------------------------------------------------

    def log_telemetry(
        self,
        tick: MeterTick,
        window_state: WindowState,
        projected_mdi_kva: float,
        tod_window: str,
    ) -> None:
        payload = {
            "event_id": str(uuid.uuid4()),
            "facility_id": tick.facility_id,
            "timestamp": tick.timestamp.isoformat(),
            "kw": tick.kw,
            "kva": tick.kva,
            "kvar": tick.kvar,
            "kvah_cumulative": tick.kvah_cumulative,
            "pf": tick.pf,
            "voltage_l1": tick.voltage_l1,
            "voltage_l2": tick.voltage_l2,
            "voltage_l3": tick.voltage_l3,
            "frequency": tick.frequency,
            "window_start": window_state.window_start.isoformat() if window_state.window_start else "",
            "accumulated_kvah_this_window": window_state.accumulated_kVAh,
            "projected_mdi_kva": projected_mdi_kva,
            "tod_window": tod_window,
            "source": tick.source,
            "polling_latency_ms": tick.polling_latency_ms,
            "data_quality": tick.data_quality,
        }
        self._wal_write("telemetry_events", payload)

    def log_decision(self, recommendation: Recommendation, risk_state: RiskState, tariff_state: TariffState) -> None:
        payload = {
            "event_id": str(uuid.uuid4()),
            "decision_id": recommendation.decision_id,
            "facility_id": recommendation.facility_id,
            "timestamp": recommendation.timestamp.isoformat(),
            "trigger": recommendation.trigger,
            "risk_level": recommendation.risk_level,
            "projected_mdi_kva": risk_state.projected_MDI_kva,
            "contract_demand_kva": risk_state.contract_demand_kva,
            "headroom_kva": risk_state.headroom_kva,
            "remaining_window_minutes": risk_state.remaining_minutes,
            "tod_window": tariff_state.tod_window,
            "billing_cycle_day": risk_state.billing_cycle_day,
            "recommendation_type": recommendation.recommendation_type,
            "loads_selected": json.dumps(recommendation.loads_selected),
            "expected_mdi_reduction_kva": recommendation.expected_mdi_reduction_kva,
            "prevented_md_kva": (
                recommendation.economic_impact.prevented_md_kva
                if recommendation.economic_impact is not None
                else None
            ),
            "projected_saving_rupees": (
                recommendation.economic_impact.projected_saving_rupees
                if recommendation.economic_impact is not None
                else None
            ),
            "economic_status": (
                recommendation.economic_impact.economic_status
                if recommendation.economic_impact is not None
                else None
            ),
            "saving_basis": (
                recommendation.economic_impact.saving_basis
                if recommendation.economic_impact is not None
                else None
            ),
            "confidence": recommendation.confidence,
            "condition_familiarity_score": None,
            "intelligent_layer_override": int(recommendation.intelligent_layer_override),
            "override_reason": recommendation.override_reason,
            "conflict_resolved": int(recommendation.conflict_resolved),
            "conflict_resolution": recommendation.conflict_resolution_detail,
        }
        self._wal_write("decision_events", payload)

    def log_window_event(self, window_state: WindowState, actual_mdi: float) -> None:
        try:
            payload = {
                "window_start": window_state.window_start.isoformat(),
                "window_end": window_state.window_end.isoformat(),
                "actual_mdi": actual_mdi,
                "accumulated_kvah": window_state.accumulated_kVAh,
                "processed": 0
            }

            self._wal_write("window_events", payload)

        except Exception as e:
            logger.error(f"Failed to log window event: {e}")

    def log_outcome_event(self, outcome: dict):
        try:
            outcome.setdefault("confirmation_source", None)
            outcome.setdefault("confirmed_at", None)
            outcome.setdefault("measured_delta_kva", None)
            outcome.setdefault("confirmation_latency_ms", None)
            outcome.setdefault("total_loads", None)
            outcome.setdefault("followed_loads", None)
            outcome.setdefault("ignored_loads", None)
            outcome.setdefault("compliance_pct", None)
            self._wal_write("outcome_events", outcome)
        except Exception as e:
            logger.error(f"Failed to log outcome event: {e}")

    def log_window_outcome(self, outcome: dict):
        try:
            self._wal_write(
                "window_outcomes",
                outcome,
            )

        except Exception as e:
            logger.error(
                f"Failed to log window outcome: {e}"
            )

    def log_execution_event(self, payload: dict):
        try:
            self._wal_write("execution_events", payload)
        except Exception as e:
            logger.warning(f"Execution logging failed: {e}")

    def mark_window_processed(self, event_id: int):
        payload = {"id": event_id}
        self._wal_write("window_events_processed", payload)