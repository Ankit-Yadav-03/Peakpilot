from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import logging


from core.models import MeterTick, Recommendation
from decision.equipment_state_manager import EquipmentQuality, EquipmentStateManager
from learning.event_logger import DurableEventLogger


logger = logging.getLogger(__name__)


class ExecutionStatus(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    SHED_CONFIRMED = "SHED_CONFIRMED"
    EXECUTION_NOT_CONFIRMED = "EXECUTION_NOT_CONFIRMED"
    PENDING_RESTORE_CONFIRMATION = "PENDING_RESTORE_CONFIRMATION"
    RESTORE_CONFIRMED = "RESTORE_CONFIRMED"
    RESTORE_NOT_CONFIRMED = "RESTORE_NOT_CONFIRMED"


class ExecutionCommand(str, Enum):
    SHED = "SHED"
    RESTORE = "RESTORE"


TERMINAL_STATUSES = {
    ExecutionStatus.SHED_CONFIRMED,
    ExecutionStatus.EXECUTION_NOT_CONFIRMED,
    ExecutionStatus.RESTORE_CONFIRMED,
    ExecutionStatus.RESTORE_NOT_CONFIRMED,
}


@dataclass
class ExecutionRecord:
    event_id: str
    decision_id: str
    facility_id: str
    load_id: str
    command_type: ExecutionCommand
    expected_state: str
    status: ExecutionStatus
    issued_at: datetime
    updated_at: datetime
    expected_delta_kva: float
    pre_kva: float
    pre_equipment_running: Optional[bool]
    confirmed_at: Optional[datetime] = None
    confirmation_latency_ms: Optional[float] = None
    confirmation_source: Optional[str] = None
    post_kva: Optional[float] = None
    measured_delta_kva: Optional[float] = None
    post_equipment_running: Optional[bool] = None
    equipment_last_update: Optional[datetime] = None
    equipment_quality: Optional[str] = None
    telemetry_quality: Optional[str] = None
    failure_reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class ExecutionStateManager:
    CONFIRMATION_TIMEOUT_SEC = 180
    MIN_CONFIRMATION_DELAY_SEC = 10
    MIN_AGGREGATE_DELTA_KVA = 5.0
    AGGREGATE_DELTA_RATIO = 0.50
    RESTORE_AGGREGATE_RATIO = 0.25

    def __init__(
        self,
        event_logger: DurableEventLogger,
        equipment_manager: EquipmentStateManager,
        equipment_freshness_sec: int,
    ) -> None:
        self._event_logger = event_logger
        self._equipment_manager = equipment_manager
        self._equipment_freshness_sec = equipment_freshness_sec
        self._records: dict[str, ExecutionRecord] = {}

    def create_pending_for_recommendation(
        self,
        recommendation: Recommendation,
        tick: MeterTick,
        command_type: ExecutionCommand,
    ) -> list[ExecutionRecord]:
        records: list[ExecutionRecord] = []
        if not recommendation.loads_selected:
            return records

        if command_type == ExecutionCommand.SHED:
            status = ExecutionStatus.PENDING_CONFIRMATION
            expected_state = "OFF"
        else:
            status = ExecutionStatus.PENDING_RESTORE_CONFIRMATION
            expected_state = "ON"

        expected_delta = self._expected_delta_per_load(recommendation)
        for load_id in recommendation.loads_selected:
            if self._has_open_record(
                recommendation.decision_id,
                load_id,
                command_type,
            ):
                continue

            equipment_state = self._equipment_manager.get_state(load_id)
            record = ExecutionRecord(
                event_id=str(uuid.uuid4()),
                decision_id=recommendation.decision_id,
                facility_id=recommendation.facility_id,
                load_id=load_id,
                command_type=command_type,
                expected_state=expected_state,
                status=status,
                issued_at=tick.timestamp,
                updated_at=tick.timestamp,
                expected_delta_kva=expected_delta,
                pre_kva=tick.kva,
                pre_equipment_running=(
                    equipment_state.is_running
                    if equipment_state is not None
                    else None
                ),
                equipment_last_update=(
                    equipment_state.last_update
                    if equipment_state is not None
                    else None
                ),
                equipment_quality=(
                    equipment_state.quality.value
                    if equipment_state is not None
                    else None
                ),
                telemetry_quality=tick.data_quality,
                metadata={
                    "recommendation_type": recommendation.recommendation_type,
                    "risk_level": recommendation.risk_level,
                },
            )
            self._records[record.event_id] = record
            self._persist(record)
            records.append(record)

        return records

    def reconcile(self, tick: MeterTick) -> list[ExecutionRecord]:
        changed: list[ExecutionRecord] = []

        for record in list(self._records.values()):
            if record.status in TERMINAL_STATUSES:
                continue

            equipment_state = self._equipment_manager.get_state(record.load_id)
            equipment_fresh = self._is_equipment_fresh(equipment_state, tick.timestamp)
            equipment_confirms = False
            if equipment_state is not None and equipment_fresh:
                if (
                    record.command_type == ExecutionCommand.SHED
                    and equipment_state.is_running is False
                ):
                    equipment_confirms = True
                elif (
                    record.command_type == ExecutionCommand.RESTORE
                    and equipment_state.is_running is True
                ):
                    equipment_confirms = True

            measured_delta = (
                record.pre_kva - tick.kva
            )

            meter_confirms = False
            if record.command_type == ExecutionCommand.SHED:
                threshold = max(
                    self.MIN_AGGREGATE_DELTA_KVA,
                    record.expected_delta_kva * self.AGGREGATE_DELTA_RATIO,
                )
                meter_confirms = (
                    measured_delta >= threshold
                )

            elapsed_sec = (
                tick.timestamp - record.issued_at
            ).total_seconds()

            timed_out = (
                elapsed_sec >= self.CONFIRMATION_TIMEOUT_SEC
            )

            stabilized = (
                elapsed_sec >= self.MIN_CONFIRMATION_DELAY_SEC
            )

            if record.command_type == ExecutionCommand.SHED:
                if equipment_confirms or (stabilized and meter_confirms):
                    record.status = ExecutionStatus.SHED_CONFIRMED

                    logger.info(
                        "[EXECUTION_CONFIRMED] decision=%s load=%s source=%s delta=%.1f",
                        record.decision_id,
                        record.load_id,
                        record.confirmation_source,
                        measured_delta,
                    )

                    record.confirmed_at = tick.timestamp
                    record.confirmation_source = self._confirmation_source(
                        equipment_confirms,
                        meter_confirms,
                    )
                elif timed_out:
                    record.status = ExecutionStatus.EXECUTION_NOT_CONFIRMED

                    logger.warning(
                        "[EXECUTION_TIMEOUT] decision=%s load=%s reason=%s",
                        record.decision_id,
                        record.load_id,
                        record.failure_reason,
                    )

                    record.confirmed_at = tick.timestamp
                    record.confirmation_source = "TIMEOUT"
                    record.failure_reason = self._failure_reason(equipment_state, equipment_fresh)
                else:
                    continue
            else:
                restore_meter_confirms = (
                    measured_delta <= (
                        -record.expected_delta_kva
                        * self.RESTORE_AGGREGATE_RATIO
                    )
                )

                if equipment_confirms or (
                    stabilized
                    and restore_meter_confirms
                ):
                    record.status = ExecutionStatus.RESTORE_CONFIRMED
                    
                    logger.info(
                        "[EXECUTION_CONFIRMED] decision=%s load=%s source=%s delta=%.1f",
                        record.decision_id,
                        record.load_id,
                        record.confirmation_source,
                        measured_delta,
                    )

                    record.confirmed_at = tick.timestamp
                    record.confirmation_source = self._confirmation_source(
                        equipment_confirms,
                        restore_meter_confirms,
                    )
                elif timed_out:
                    record.status = ExecutionStatus.RESTORE_NOT_CONFIRMED

                    logger.warning(
                        "[EXECUTION_TIMEOUT] decision=%s load=%s reason=%s",
                        record.decision_id,
                        record.load_id,
                        record.failure_reason,
                    )

                    record.confirmed_at = tick.timestamp
                    record.confirmation_source = "TIMEOUT"
                    record.failure_reason = self._failure_reason(equipment_state, equipment_fresh)
                else:
                    continue

            record.updated_at = tick.timestamp
            record.post_kva = tick.kva
            record.measured_delta_kva = measured_delta
            record.post_equipment_running = (
                equipment_state.is_running
                if equipment_state is not None
                else None
            )
            record.equipment_last_update = (
                equipment_state.last_update
                if equipment_state is not None
                else None
            )
            record.equipment_quality = (
                equipment_state.quality.value
                if equipment_state is not None
                else None
            )
            record.telemetry_quality = tick.data_quality
            if record.confirmed_at is not None:
                record.confirmation_latency_ms = (
                    record.confirmed_at - record.issued_at
                ).total_seconds() * 1000
            self._persist(record)
            changed.append(record)

        return changed

    def get_records_for_decision(self, decision_id: str) -> list[ExecutionRecord]:
        return [
            record
            for record in self._records.values()
            if record.decision_id == decision_id
        ]

    def get_pending_records(self) -> list[ExecutionRecord]:
        return [
            record
            for record in self._records.values()
            if record.status not in TERMINAL_STATUSES
        ]

    def get_latest_status_by_decision(self, decision_id: str) -> dict:
        records = self.get_records_for_decision(decision_id)
        if not records:
            return {}

        latest = max(records, key=lambda record: record.updated_at)
        return {
            "decision_id": latest.decision_id,
            "status": latest.status.value,
            "command_type": latest.command_type.value,
            "load_id": latest.load_id,
            "confirmation_source": latest.confirmation_source,
            "confirmation_latency_ms": latest.confirmation_latency_ms,
            "measured_delta_kva": latest.measured_delta_kva,
            "confirmed_at": (
                latest.confirmed_at.isoformat()
                if latest.confirmed_at is not None
                else None
            ),
            "pending_count": len([
                record
                for record in records
                if record.status not in TERMINAL_STATUSES
            ]),
            "records": [self._public_record(record) for record in records],
        }

    def clear_terminal_records(self, now: datetime, retention_sec: int = 3600) -> None:
        for event_id, record in list(self._records.items()):
            if record.status not in TERMINAL_STATUSES:
                continue

            if (now - record.updated_at).total_seconds() < retention_sec:
                continue
            
            record.updated_at = now
            del self._records[event_id]

    def has_open_command(
        self,
        load_id: str,
        command_type: ExecutionCommand,
    ) -> bool:

        return any(
            record.load_id == load_id
            and record.command_type == command_type
            and record.status not in TERMINAL_STATUSES
            for record in self._records.values()
        )

    def _has_open_record(
        self,
        decision_id: str,
        load_id: str,
        command_type: ExecutionCommand,
    ) -> bool:
        return any(
            record.decision_id == decision_id
            and record.load_id == load_id
            and record.command_type == command_type
            and record.status not in TERMINAL_STATUSES
            for record in self._records.values()
        )

    def _expected_delta_per_load(self, recommendation: Recommendation) -> float:
        if not recommendation.loads_selected:
            return 0.0
        return (
            recommendation.expected_mdi_reduction_kva
            / len(recommendation.loads_selected)
        )

    def _is_equipment_fresh(self, equipment_state, current_time: datetime) -> bool:
        if equipment_state is None:
            return False
        if equipment_state.quality != EquipmentQuality.GOOD:
            return False
        return (
            current_time - equipment_state.last_update
        ).total_seconds() <= self._equipment_freshness_sec

    def _confirmation_source(
        self,
        equipment_confirms: bool,
        meter_confirms: bool,
    ) -> str:
        if equipment_confirms and meter_confirms:
            return "EQUIPMENT_AND_METER"
        if equipment_confirms:
            return "EQUIPMENT"
        if meter_confirms:
            return "METER"
        return "UNKNOWN"

    def _failure_reason(self, equipment_state, equipment_fresh: bool) -> str:
        if equipment_state is None or not equipment_fresh:
            return "STALE_OR_MISSING_EQUIPMENT_TELEMETRY"
        return "TELEMETRY_DID_NOT_MATCH_EXPECTED_STATE"

    def _persist(self, record: ExecutionRecord) -> None:
        self._event_logger.log_execution_event({
            "event_id": record.event_id,
            "decision_id": record.decision_id,
            "facility_id": record.facility_id,
            "load_id": record.load_id,
            "command_type": record.command_type.value,
            "expected_state": record.expected_state,
            "status": record.status.value,
            "issued_at": record.issued_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "confirmed_at": (
                record.confirmed_at.isoformat()
                if record.confirmed_at is not None
                else None
            ),
            "confirmation_latency_ms": record.confirmation_latency_ms,
            "confirmation_source": record.confirmation_source,
            "pre_equipment_running": self._bool_to_int(record.pre_equipment_running),
            "post_equipment_running": self._bool_to_int(record.post_equipment_running),
            "equipment_last_update": (
                record.equipment_last_update.isoformat()
                if record.equipment_last_update is not None
                else None
            ),
            "equipment_quality": record.equipment_quality,
            "pre_kva": record.pre_kva,
            "post_kva": record.post_kva,
            "expected_delta_kva": record.expected_delta_kva,
            "measured_delta_kva": record.measured_delta_kva,
            "telemetry_quality": record.telemetry_quality,
            "failure_reason": record.failure_reason,
            "metadata": json.dumps(record.metadata),
        })

    def _public_record(self, record: ExecutionRecord) -> dict:
        return {
            "event_id": record.event_id,
            "load_id": record.load_id,
            "command_type": record.command_type.value,
            "status": record.status.value,
            "confirmation_source": record.confirmation_source,
            "confirmation_latency_ms": record.confirmation_latency_ms,
            "measured_delta_kva": record.measured_delta_kva,
            "failure_reason": record.failure_reason,
        }

    def _bool_to_int(self, value: Optional[bool]) -> Optional[int]:
        if value is None:
            return None
        return int(value)
