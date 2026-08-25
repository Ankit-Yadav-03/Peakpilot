from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from core.config import IST, FacilityConfig
from decision.equipment_state_manager import (
    EquipmentStateManager,
    EquipmentSource,
    EquipmentQuality,
)


class SimulatedEquipmentTelemetry:

    def __init__(
        self,
        facility_config: FacilityConfig,
        equipment_manager: EquipmentStateManager,
        compliance_probability: float = 0.7,   
        random_seed: Optional[int] = None,      
    ):
        self._facility = facility_config
        self._equipment_manager = equipment_manager
        self._compliance_probability = max(0.0, min(1.0, compliance_probability))
        self._rng = random.Random(random_seed)                         

    def initialize(self) -> None:

        now = datetime.now(IST)

        for load in self._facility.loads_raw["loads"]:

            self._equipment_manager.update_state(
                load_id=load["load_id"],
                is_running=True,
                timestamp=now,
                source=EquipmentSource.MANUAL,
                quality=EquipmentQuality.GOOD,
            )

    def shed(
        self,
        load_ids: list[str],
    ) -> None:

        now = datetime.now(IST)

        for load_id in load_ids:

            if self._rng.random() > self._compliance_probability:     
                continue                                               

            self._equipment_manager.update_state(
                load_id=load_id,
                is_running=False,
                timestamp=now,
                source=EquipmentSource.MANUAL,
                quality=EquipmentQuality.GOOD,
            )

    def restore(
        self,
        load_ids: list[str],
    ) -> None:

        now = datetime.now(IST)

        for load_id in load_ids:

            if self._rng.random() > self._compliance_probability:     
                continue                                               

            self._equipment_manager.update_state(
                load_id=load_id,
                is_running=True,
                timestamp=now,
                source=EquipmentSource.MANUAL,
                quality=EquipmentQuality.GOOD,
            )