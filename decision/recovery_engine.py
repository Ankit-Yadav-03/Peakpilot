from __future__ import annotations

from datetime import datetime, timedelta
import logging

from core.models import RiskState
from decision.action_state_manager import ActionStateManager
from decision.equipment_state_manager import EquipmentStateManager

logger = logging.getLogger(__name__)

class RecoveryEngine:

    def __init__(self):
        self._last_restore_time = None
        self._restore_cooldown = timedelta(minutes=5)   # for 30-min demand window

    def evaluate(
        self,
        action_manager: ActionStateManager,
        equipment_manager: EquipmentStateManager,
        risk_state: RiskState,
        current_time: datetime
    ) -> list[str]:

        if risk_state.risk_level not in ["SAFE", "NORMAL"]:
            return []

        # cooldown suppresses restore chatter
        if (
            self._last_restore_time is not None
            and
            (current_time - self._last_restore_time) < self._restore_cooldown
        ):
            return []

        restore_candidates = (
            action_manager.get_restore_pending_loads(
                current_time
            )
        )

        logger.warning(
            "[RESTORE_DEBUG] risk=%s restore_candidates=%s",
            risk_state.risk_level,
            restore_candidates,
        )

        if not restore_candidates:
            return []

        eligible_loads = []

        for load_id in restore_candidates:

            # Still inside active shed duration
            if action_manager.is_active(
                load_id,
                current_time,
            ):
                continue

            # Real telemetry validation
            is_running = equipment_manager.is_running(load_id)

            if is_running is None:
                continue

            # Only restore loads still OFF
            if is_running is False:

                eligible_loads.append(load_id)

            logger.warning(
                "[RESTORE_DEBUG] load=%s active=%s running=%s",
                load_id,
                action_manager.is_active(load_id, current_time),
                equipment_manager.is_running(load_id),
            )

        if not eligible_loads:
            return []

        eligible_loads = sorted(eligible_loads)

        self._last_restore_time = current_time

        return [eligible_loads[0]]