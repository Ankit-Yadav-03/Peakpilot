from __future__ import annotations
import logging
from typing import Dict

from core.models import MeterTick, TariffState, WindowState, CostState
from core.config import TariffConfig, FacilityConfig, SystemConfig

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730.0


def compute_derc_bill(
    mdi_kva: float,
    contract_demand_kva: float,
    kvah_consumed: float,
    kwh_consumed: float,
    tariff: TariffConfig,
    discom: str,
    voltage_level: str,
    meter_rent: float = 500.0,
) -> Dict[str, float]:

    billable_demand = max(mdi_kva, contract_demand_kva)
    demand_charge = billable_demand * tariff.demand_charge_per_kVA

    if mdi_kva > contract_demand_kva:
        excess_kva = mdi_kva - contract_demand_kva
        excess_surcharge = excess_kva * tariff.demand_charge_per_kVA * 0.30
    else:
        excess_surcharge = 0.0

    voltage_rebate = tariff.voltage_rebate(voltage_level)
    rebate_factor = 1.0 - voltage_rebate
    rebated_base_rate = tariff.energy_charge_per_kVAh * rebate_factor

    energy_charge = kvah_consumed * rebated_base_rate

    subtotal_base = demand_charge + excess_surcharge + energy_charge

    drrs = subtotal_base * tariff.drrs_rate
    pension_trust = subtotal_base * tariff.pension_trust_rate
    ppac = subtotal_base * tariff.ppac_for_discom(discom)
    electricity_duty = kwh_consumed * tariff.electricity_duty_per_kWh

    total_bill = subtotal_base + drrs + pension_trust + ppac + electricity_duty + meter_rent

    return {
        "demand_charge": round(demand_charge, 2),
        "excess_surcharge": round(excess_surcharge, 2),
        "energy_charge": round(energy_charge, 2),
        "subtotal_base": round(subtotal_base, 2),
        "drrs": round(drrs, 2),
        "pension_trust": round(pension_trust, 2),
        "ppac": round(ppac, 2),
        "electricity_duty": round(electricity_duty, 2),
        "meter_rent": round(meter_rent, 2),
        "total_bill": round(total_bill, 2),
        "billable_demand_kva": round(billable_demand, 2),
        "mdi_kva": round(mdi_kva, 2),
    }


def compute_excess_cost(mdi_kva: float, contract_demand_kva: float, tariff: TariffConfig) -> float:
    if mdi_kva <= contract_demand_kva:
        return 0.0
    return (mdi_kva - contract_demand_kva) * tariff.demand_charge_per_kVA * 0.30


def compute_actual_saving(
    counterfactual_mdi: float,
    actual_mdi: float,
    contract_demand: float,
    tariff: TariffConfig,
    previous_uncontrolled_peak_kva: float = 0.0,
    previous_actual_peak_kva: float = 0.0,
) -> float:

    control_threshold = contract_demand * 0.90

    # No exposure at all
    if counterfactual_mdi <= control_threshold:
        return 0.0

    # Monthly uncontrolled exposure
    uncontrolled_peak = max(
        previous_uncontrolled_peak_kva,
        counterfactual_mdi,
    )

    # Monthly protected/actual exposure
    protected_peak = max(
        previous_actual_peak_kva,
        actual_mdi,
    )

    # Only NEW marginal protection matters
    marginal_exposure_kva = max(
        0.0,
        uncontrolled_peak - protected_peak,
    )

    if marginal_exposure_kva <= 0:
        return 0.0

    # Convert protected exposure into projected economic value
    demand_rate = tariff.demand_charge_per_kVA

    saving = (
        marginal_exposure_kva * demand_rate
    )

    return round(max(0.0, saving), 2)


def compute_projected_saving(
    counterfactual_mdi: float,
    protected_mdi: float,
    contract_demand_kva: float,
    tariff: TariffConfig,
) -> float:
    """
    Decision-time projected economic protection.

    This is NOT realized saving.
    This estimates potential avoided exposure
    if current intervention succeeds.
    """

    control_threshold = contract_demand_kva * 0.90

    # No projected exposure
    if counterfactual_mdi <= control_threshold:
        return 0.0

    uncontrolled_excess = max(
        0.0,
        counterfactual_mdi - control_threshold,
    )

    protected_excess = max(
        0.0,
        protected_mdi - control_threshold,
    )

    prevented_exposure = max(
        0.0,
        uncontrolled_excess - protected_excess,
    )

    if prevented_exposure <= 0:
        return 0.0

    demand_rate = tariff.demand_charge_per_kVA

    projected_value = (
        prevented_exposure * demand_rate
    )

    return round(
        max(0.0, projected_value),
        2,
    )

class CostSimulator:

    def __init__(
        self,
        tariff_config: TariffConfig,
        facility_config: FacilityConfig,
        system_config: SystemConfig,
    ):
        self._tariff = tariff_config
        self._facility = facility_config
        self._system = system_config
        self._meter_rent = system_config.meter_rent_monthly

        self._last_projected_bill = None
        self._accumulated_savings = 0.0

    def compute(
        self,
        tick: MeterTick,
        tariff_state: TariffState,
        window_state: WindowState,
        projected_mdi_kva: float,
        months_md_so_far_kva: float,
    ) -> CostState:

        contract_kva = self._facility.contract_demand_kva
        discom = self._facility.discom
        voltage_level = self._facility.voltage_level

        effective_mdi = max(projected_mdi_kva, months_md_so_far_kva)

        elapsed_hours = window_state.elapsed_minutes / 60.0
        if elapsed_hours > 0:
            avg_rate = window_state.accumulated_kVAh / elapsed_hours
        else:
            avg_rate = tick.kva

        # 🔴 ALIGN WITH STATE DETECTOR (CONSERVATIVE MODEL)
        kvah_rate_per_hour = max(
            avg_rate,
            tick.kva,  # latest signal
            projected_mdi_kva
        )
 
        projected_monthly_kvah = kvah_rate_per_hour * HOURS_PER_MONTH
        projected_monthly_kwh = projected_monthly_kvah * max(tick.pf, 0.1)


        bill = compute_derc_bill(
            mdi_kva=effective_mdi,
            contract_demand_kva=contract_kva,
            kvah_consumed=projected_monthly_kvah,
            kwh_consumed=projected_monthly_kwh,
            tariff=self._tariff,
            discom=discom,
            voltage_level=voltage_level,
            meter_rent=self._meter_rent,
        )

        instantaneous_rate = tick.kw * tariff_state.effective_energy_rate

        excess_cost = compute_excess_cost(
            mdi_kva=effective_mdi,
            contract_demand_kva=contract_kva,
            tariff=self._tariff,
        )

        return CostState(
            timestamp=tick.timestamp,
            instantaneous_cost_rate_per_hour=round(instantaneous_rate, 2),
            projected_monthly_bill=bill["total_bill"],
            demand_charge=bill["demand_charge"],
            excess_surcharge=bill["excess_surcharge"],
            energy_charge=bill["energy_charge"],
            drrs=bill["drrs"],
            pension_trust=bill["pension_trust"],
            ppac=bill["ppac"],
            electricity_duty=bill["electricity_duty"],
            total_bill=bill["total_bill"],
            projected_mdi_kva=round(effective_mdi, 2),
            contract_demand_kva=contract_kva,
            excess_cost=round(excess_cost, 2),
        )