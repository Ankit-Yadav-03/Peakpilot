from __future__ import annotations

import logging

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class EquipmentQuality(str, Enum):

    GOOD = "GOOD"

    BAD = "BAD"

    UNCERTAIN = "UNCERTAIN"


class EquipmentSource(str, Enum):

    SCADA = "SCADA"

    PLC = "PLC"

    MQTT = "MQTT"

    MODBUS = "MODBUS"

    MANUAL = "MANUAL"


@dataclass
class EquipmentState:

    load_id: str

    is_running: bool

    last_changed: datetime

    source: EquipmentSource

    quality: EquipmentQuality

    last_update: datetime


class EquipmentStateManager:

    def __init__(self) -> None:

        self._states: Dict[str, EquipmentState] = {}

    def update_state(
        self,
        load_id: str,
        is_running: bool,
        timestamp: datetime,
        source: EquipmentSource = EquipmentSource.SCADA,
        quality: EquipmentQuality = EquipmentQuality.GOOD,
    ) -> None:

        if timestamp.tzinfo is None:
            raise ValueError(
                "timestamp must be timezone aware"
            )
        
        if not load_id:
            raise ValueError(
                "load_id cannot be empty"
            )

        previous = self._states.get(load_id)

        # Reject stale timestamps.
        # Equal timestamps are allowed because some
        # telemetry sources may resend identical state.
        if previous is not None:

            if timestamp < previous.last_update:

                logger.warning(
                    "Ignoring stale equipment telemetry "
                    "for %s: %s < %s",
                    load_id,
                    timestamp,
                    previous.last_update,
                )

                return

        changed = (
            previous is None
            or previous.is_running != is_running
        )

        if changed:
            last_changed = timestamp
        else:
            last_changed = (
                previous.last_changed
                if previous is not None
                else timestamp
            )

        self._states[load_id] = EquipmentState(
            load_id=load_id,
            is_running=is_running,
            last_changed=last_changed,
            source=source,
            quality=quality,
            last_update=timestamp,
        )

    def is_running(
        self,
        load_id: str,
    ) -> Optional[bool]:

        state = self._states.get(load_id)

        return (
            state.is_running
            if state is not None
            else None
        )

    def get_state(
        self,
        load_id: str,
    ) -> Optional[EquipmentState]:

        return self._states.get(load_id)

    def get_running_loads(self) -> list[str]:

        return sorted(
            load_id
            for load_id, state in self._states.items()
            if state.is_running
        )

    def get_stopped_loads(self) -> list[str]:

        return sorted(
            load_id
            for load_id, state in self._states.items()
            if not state.is_running
        )

    def has_state(
        self,
        load_id: str,
    ) -> bool:

        return load_id in self._states

    def clear_state(
        self,
        load_id: str,
    ) -> None:

        if load_id in self._states:
            del self._states[load_id]

    def clear_all(self) -> None:

        self._states.clear()