from __future__ import annotations

from core.config import FacilityConfig
from decision.equipment_state_manager import EquipmentStateManager


class SimulatedPlantModel:

    LOAD_EFFECTIVENESS = 0.80

    def __init__(
        self,
        facility_config: FacilityConfig,
        equipment_manager: EquipmentStateManager,
    ):
        self._equipment_manager = equipment_manager

        self._load_kva: dict[str, float] = {}

        for load in facility_config.loads_raw["loads"]:

            typical_kw = float(load["typical_kw"])
            pf = float(load["typical_pf"])

            if pf <= 0:
                continue

            self._load_kva[
                load["load_id"]
            ] = typical_kw / pf

    def shed_reduction_kva(self) -> float:

        total = 0.0

        for load_id, kva in self._load_kva.items():

            state = self._equipment_manager.get_state(
                load_id
            )

            if state is None:
                continue

            if state.is_running is False:
                total += kva

        return (
            total
            * self.LOAD_EFFECTIVENESS
        )