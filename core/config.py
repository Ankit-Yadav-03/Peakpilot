"""
core/config.py
Configuration dataclasses and loader.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Dict, List
from datetime import timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

@dataclass
class ShiftSchedule:
    shift_1_start: str      # HH:MM
    shift_2_start: str      # HH:MM
    shift_end: str          # HH:MM

    def to_minutes(self, hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    @property
    def shift_1_start_minutes(self) -> int:
        return self.to_minutes(self.shift_1_start)

    @property
    def shift_2_start_minutes(self) -> int:
        return self.to_minutes(self.shift_2_start)

    @property
    def shift_end_minutes(self) -> int:
        return self.to_minutes(self.shift_end)


@dataclass
class FacilityConfig:
    facility_id: str
    facility_name: str
    contract_demand_kva: float
    discom: str             # BRPL / BYPL / TPDDL
    voltage_level: str      # 11kv / 33kv / 220kv
    shift_schedule: ShiftSchedule
    loads_path: str
    loads_raw: Dict = field(default_factory=dict)


@dataclass
class TariffConfig:
    demand_charge_per_kVA: float
    excess_surcharge_rate: float
    energy_charge_per_kVAh: float
    voltage_rebate_11kv: float
    voltage_rebate_33kv: float
    voltage_rebate_220kv: float
    tod_peak_multiplier: float
    tod_offpeak_multiplier: float
    drrs_rate: float
    pension_trust_rate: float
    ppac_brpl: float
    ppac_bypl: float
    ppac_tpddl: float
    electricity_duty_per_kWh: float
    tod_applicable_months: List[int]
    peak_hours_summer: List[List[int]]
    offpeak_hours_summer: List[List[int]]

    def ppac_for_discom(self, discom: str) -> float:
        mapping = {
            "BRPL": self.ppac_brpl,
            "BYPL": self.ppac_bypl,
            "TPDDL": self.ppac_tpddl,
        }
        if discom not in mapping:
            raise ValueError(f"Unknown DISCOM: {discom}. Must be BRPL, BYPL, or TPDDL.")
        return mapping[discom]

    def voltage_rebate(self, voltage_level: str) -> float:
        mapping = {
            "11kv": self.voltage_rebate_11kv,
            "33kv": self.voltage_rebate_33kv,
            "220kv": self.voltage_rebate_220kv,
        }
        if voltage_level not in mapping:
            raise ValueError(f"Unknown voltage level: {voltage_level}")
        return mapping[voltage_level]


@dataclass
class SystemConfig:
    db_path: str
    polling_interval_seconds: int
    max_consecutive_failures: int
    safety_margin_kva: float
    decision_cooldown_minutes: int
    inrush_window_seconds: int
    load_creep_threshold_pct: float
    load_creep_consecutive_windows: int
    stale_data_threshold_seconds: int
    simulation_tick_interval_seconds: float
    simulation_duration_minutes: int
    api_host: str
    api_port: int
    log_level: str
    meter_rent_monthly: float
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic: str = "energy/equipment/#"


def load_tariff_config(path: str) -> TariffConfig:
    with open(path, "r") as f:
        raw = json.load(f)
    return TariffConfig(
        demand_charge_per_kVA=raw["demand_charge_per_kVA"],
        excess_surcharge_rate=raw["excess_surcharge_rate"],
        energy_charge_per_kVAh=raw["energy_charge_per_kVAh"],
        voltage_rebate_11kv=raw["voltage_rebate_11kv"],
        voltage_rebate_33kv=raw["voltage_rebate_33kv"],
        voltage_rebate_220kv=raw["voltage_rebate_220kv"],
        tod_peak_multiplier=raw["tod_peak_multiplier"],
        tod_offpeak_multiplier=raw["tod_offpeak_multiplier"],
        drrs_rate=raw["drrs_rate"],
        pension_trust_rate=raw["pension_trust_rate"],
        ppac_brpl=raw["ppac_brpl"],
        ppac_bypl=raw["ppac_bypl"],
        ppac_tpddl=raw["ppac_tpddl"],
        electricity_duty_per_kWh=raw["electricity_duty_per_kWh"],
        tod_applicable_months=raw["tod_applicable_months"],
        peak_hours_summer=raw["peak_hours_summer"],
        offpeak_hours_summer=raw["offpeak_hours_summer"],
    )


def load_facility_config(path: str) -> FacilityConfig:
    with open(path, "r") as f:
        raw = json.load(f)
    if raw["contract_demand_kva"] <= 0:
        raise ValueError(
            "contract_demand_kva must be positive"
        )

    if raw["discom"] not in {
        "BRPL",
        "BYPL",
        "TPDDL",
    }:
        raise ValueError(
            f"Unsupported DISCOM: {raw['discom']}"
        )

    if raw.get("voltage_level", "11kv") not in {
        "11kv",
        "33kv",
        "220kv",
    }:
        raise ValueError(
            f"Unsupported voltage level: {raw['voltage_level']}"
        )
    ss = raw["shift_schedule"]
    return FacilityConfig(
        facility_id=raw["facility_id"],
        facility_name=raw["facility_name"],
        contract_demand_kva=raw["contract_demand_kva"],
        discom=raw["discom"],
        voltage_level=raw.get("voltage_level", "11kv"),
        shift_schedule=ShiftSchedule(
            shift_1_start=ss["shift_1_start"],
            shift_2_start=ss["shift_2_start"],
            shift_end=ss["shift_end"],
        ),
        loads_path=path,
        loads_raw=raw,
    )


def load_system_config(path: str) -> SystemConfig:
    import yaml
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    sys = raw.get("system", {})
    if sys.get("polling_interval_seconds", 30) <= 0:
        raise ValueError(
            "polling_interval_seconds must be > 0"
        )

    if sys.get("api_port", 8000) <= 0:
        raise ValueError(
            "api_port must be > 0"
        )
    return SystemConfig(
        db_path=sys.get("db_path", "Peakpilot.db"),
        polling_interval_seconds=sys.get("polling_interval_seconds", 30),
        max_consecutive_failures=sys.get("max_consecutive_failures", 5),
        safety_margin_kva=sys.get("safety_margin_kva", 15.0),
        decision_cooldown_minutes=sys.get("decision_cooldown_minutes", 2),
        inrush_window_seconds=sys.get("inrush_window_seconds", 90),
        load_creep_threshold_pct=sys.get("load_creep_threshold_pct", 3.0),
        load_creep_consecutive_windows=sys.get("load_creep_consecutive_windows", 3),
        stale_data_threshold_seconds=sys.get("stale_data_threshold_seconds", 90),
        simulation_tick_interval_seconds=sys.get("simulation_tick_interval_seconds", 15),
        simulation_duration_minutes=sys.get("simulation_duration_minutes", 60),
        api_host=sys.get("api_host", "0.0.0.0"),
        api_port=sys.get("api_port", 8000),
        log_level=sys.get("log_level", "INFO"),
        meter_rent_monthly=sys.get("meter_rent_monthly", 500.0),
        mqtt_host=sys.get("mqtt_host", "localhost"),
        mqtt_port=sys.get("mqtt_port", 1883),
        mqtt_topic=sys.get("mqtt_topic", "energy/equipment/#"),
    )
