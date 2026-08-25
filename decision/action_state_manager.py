from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict

logger = logging.getLogger(__name__)


class ActionType:
    SHED = "SHED"


@dataclass
class LoadActionState:
    load_id: str
    state: str
    action_start: datetime
    expected_duration_sec: int
    cooldown_until: datetime
    source: str
    decision_id: str | None = None
    execution_event_id: str | None = None
    execution_confirmed: bool = False
    confirmed_at: datetime | None = None
    archived: bool = False

    @property
    def end_time(self) -> datetime:
        return self.action_start + timedelta(seconds=self.expected_duration_sec)


class ActionStateManager:
    HISTORICAL_RETENTION_MINUTES = 30

    def __init__(self) -> None:
        self._active_states: Dict[str, LoadActionState] = {}

    def mark_shed_confirmed(
        self,
        load_ids: list[str],
        duration_sec: int,
        confirmed_at: datetime,
        decision_id: str,
        execution_event_ids: dict[str, str],
        cooldown_minutes: int = 2,
    ) -> None:
        if duration_sec <= 0:
            raise ValueError("duration_sec must be > 0")

        self.cleanup_expired(confirmed_at)

        for load_id in load_ids:
            if not self.can_activate(load_id, confirmed_at):
                continue

            end_time = confirmed_at + timedelta(seconds=duration_sec)

            self._active_states[load_id] = LoadActionState(
                load_id=load_id,
                state=ActionType.SHED,
                action_start=confirmed_at,
                expected_duration_sec=duration_sec,
                cooldown_until=end_time + timedelta(minutes=cooldown_minutes),
                source="TELEMETRY",
                decision_id=decision_id,
                execution_event_id=execution_event_ids.get(load_id),
                execution_confirmed=True,
                confirmed_at=confirmed_at,
            )

    def is_active(self, load_id: str, current_time: datetime) -> bool:
        state = self._active_states.get(load_id)
        if state is None:
            return False

        return current_time < state.end_time
    
    def get_state(
        self,
        load_id: str,
    ) -> LoadActionState | None:

        return self._active_states.get(load_id)

    def get_active_loads(self, current_time: datetime) -> list[str]:
        self.cleanup_expired(current_time)

        return sorted([
            load_id
            for load_id in self._active_states
            if (
                self.is_active(load_id, current_time)
                and not self._active_states[load_id].archived
            )
        ])

    def can_activate(self, load_id: str, current_time: datetime) -> bool:

        self.cleanup_expired(current_time)

        state = self._active_states.get(load_id)

        if state is None:
            return True

        return state.archived
    
    def awaiting_restore(
        self,
        load_id: str,
        current_time: datetime,
    ) -> bool:

        state = self._active_states.get(load_id)

        if state is None:
            return False

        if state.archived:
            return False

        return current_time >= state.end_time
    
    def get_restore_pending_loads(
        self,
        current_time: datetime,
    ) -> list[str]:

        self.cleanup_expired(current_time)

        return sorted([
            load_id
            for load_id in self._active_states
            if self.awaiting_restore(
                load_id,
                current_time,
            )
        ])
    
    def get_operationally_blocked_loads(
        self,
        current_time: datetime,
    ) -> list[str]:

        self.cleanup_expired(current_time)

        return sorted([
            load_id
            for load_id, state in self._active_states.items()
            if not state.archived
        ])

    def restore(
        self,
        load_ids: list[str],
    ) -> None:
        logger.warning("restore() is deprecated; using mark_restore_confirmed()")
        self.mark_restore_confirmed(load_ids)

    def mark_restore_confirmed(
        self,
        load_ids: list[str],
    ) -> None:

        for load_id in load_ids:
            state = self._active_states.get(load_id)

            if state is None:

                logger.warning(
                    "Restore confirmation without active state | load=%s",
                    load_id,
                )

                continue

            if not state.execution_confirmed:

                logger.warning(
                    "Restore attempted on unconfirmed state | load=%s",
                    load_id,
                )

                continue


            logger.info(
                "Restore confirmed | archiving operational lineage | load=%s",
                load_id,
            )
            state.archived = True

    def cleanup_expired(self, current_time: datetime) -> None:

        expired_load_ids = []

        for load_id, state in self._active_states.items():
            # -------------------------------------------------
            # Phase 1:
            # archived retention expired
            # purge historical memory
            # -------------------------------------------------

            if state.archived:

                purge_after = (
                    state.cooldown_until
                    + timedelta(
                        minutes=self.HISTORICAL_RETENTION_MINUTES
                    )
                )

                if current_time >= purge_after:
                    expired_load_ids.append(load_id)

        for load_id in expired_load_ids:

            logger.info(
                "Purging archived operational state | load=%s",
                load_id,
            )

            del self._active_states[load_id]
