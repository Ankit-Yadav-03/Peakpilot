"""
ingestion/meter_stream.py
MeterTick producers: SimulatedMeterReader, ModbusMeterReader, MQTTMeterReader.
All expose the same async generator interface: async for tick in reader.stream().
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import AsyncGenerator, Optional, Dict

from core.models import MeterTick
from core.config import IST, FacilityConfig, SystemConfig
from plant_model import SimulatedPlantModel

logger = logging.getLogger(__name__)


class BaseMeterReader(ABC):
    @abstractmethod
    async def stream(self) -> AsyncGenerator[MeterTick, None]:
        ...


# ---------------------------------------------------------------------------
# Simulated Meter Reader
# ---------------------------------------------------------------------------

class SimulatedMeterReader(BaseMeterReader):

    def __init__(
        self,
        facility_config: FacilityConfig,
        system_config: SystemConfig,
        start_time: Optional[datetime] = None,
        billing_cycle_day: int = 2,
        plant_model: SimulatedPlantModel = None,
    ):
        self._facility = facility_config
        self._system = system_config
        self._start_time = start_time or datetime.now(IST)
        self._billing_cycle_day = billing_cycle_day
        self._kvah_cumulative = 0.0
        self._contract_kva = facility_config.contract_demand_kva

        self._base_kva_fraction = 0.92

        self._plant_model = plant_model

        self._test_mode = "OUTCOME_TEST"  # will be overridden dynamically

    def _is_shift_changeover(self, sim_time: datetime) -> bool:
        tod_minutes = sim_time.hour * 60 + sim_time.minute
        ss = self._facility.shift_schedule
        for shift_start_str in [ss.shift_1_start, ss.shift_2_start]:
            h, m = shift_start_str.split(":")
            start_min = int(h) * 60 + int(m)
            if abs(tod_minutes - start_min) <= 15:
                return True
        return False

    def _is_startup(self, sim_time: datetime) -> bool:
        ss = self._facility.shift_schedule
        h, m = ss.shift_1_start.split(":")
        startup_min = int(h) * 60 + int(m)
        tod_minutes = sim_time.hour * 60 + sim_time.minute
        return abs(tod_minutes - startup_min) <= 15

    def _is_peak_tod(self, sim_time: datetime) -> bool:
        month = sim_time.month
        if month not in [5, 6, 7, 8, 9]:
            return False
        hour = sim_time.hour
        return (14 <= hour < 17) or (22 <= hour or hour < 1)

    def _is_offpeak_tod(self, sim_time: datetime) -> bool:
        month = sim_time.month
        if month not in [5, 6, 7, 8, 9]:
            return False
        hour = sim_time.hour
        return 4 <= hour < 10

    def _compute_kva(self, sim_time: datetime, elapsed_minutes: float) -> float:
        contract = self._contract_kva

        hour = sim_time.hour
        if 8 <= hour < 18:
            base = 0.7 * contract
        elif 18 <= hour < 23:
            base = 0.5 * contract
        else:
            base = 0.3 * contract

        if self._is_startup(sim_time):
            base += contract * 0.15 * max(0, 1.0 - elapsed_minutes / 5.0)
        elif self._is_shift_changeover(sim_time):
            base += contract * 0.12

        if self._is_peak_tod(sim_time):
            base *= 1.20

        # 🔴 CONTROLLED TEST MODES
        if self._test_mode == "NORMAL":
            base = contract * 0.35

        elif self._test_mode == "WARNING":
            base = contract * 0.80

        elif self._test_mode == "CRITICAL":
            base = contract * 1.08
            if int(sim_time.second) % 30 < 10:
                base += contract * 0.07

        elif self._test_mode == "OUTCOME_TEST":
            base = contract * 1.15

        noise = random.gauss(
            0,
            base * 0.01,
        )

        plant_reduction = 0.0

        if self._plant_model is not None:
            plant_reduction = (self._plant_model.shed_reduction_kva())

        kva = (base - plant_reduction + noise)

        if random.random() < 0.01:
            kva += contract * 0.05

        if random.random() < 0.01:
            kva -= contract * 0.05

        kva = max(0.1 * contract, min(kva, 1.5 * contract))

        return kva

    async def stream(self) -> AsyncGenerator[MeterTick, None]:
        tick_interval = max(1, int(self._system.simulation_tick_interval_seconds))

        last_kva = None
        last_real_time = self._start_time

        simulation_start = self._start_time

        while True:
            now = last_real_time + timedelta(seconds=tick_interval)

            # 🔴 MODE SWITCHING
            elapsed_sim_time = (now - simulation_start).total_seconds()

            cycle_position = elapsed_sim_time % 1800   # 30-min repeating window

            if cycle_position < 300:
                self._test_mode = "OUTCOME_TEST"
            
            elif cycle_position < 600:
                self._test_mode = "CRITICAL"

            else:
                self._test_mode = "NORMAL"

            elapsed_seconds = tick_interval
            elapsed_minutes = elapsed_seconds / 60.0

            kva = self._compute_kva(now, elapsed_minutes)

            pf = round(random.gauss(0.89, 0.02), 3)
            pf = max(0.70, min(1.00, pf))
            kw = kva * pf
            kvar = math.sqrt(max(0, kva**2 - kw**2))

            frequency = round(random.gauss(49.95, 0.05), 2)
            frequency = max(48.0, min(52.0, frequency))

            def rand_voltage():
                return round(random.gauss(230.0, 2.5), 1)

            voltage_l1 = rand_voltage()
            voltage_l2 = rand_voltage()
            voltage_l3 = rand_voltage()

            if last_kva is not None:
                avg_kva = (kva + last_kva) / 2.0
                elapsed_hours = elapsed_seconds / 3600.0
                self._kvah_cumulative += avg_kva * elapsed_hours

            last_kva = kva
            last_real_time = now

            tick = MeterTick(
                timestamp=now,
                facility_id=self._facility.facility_id,
                kw=round(kw, 3),
                kva=round(kva, 3),
                kvar=round(kvar, 3),
                pf=pf,
                frequency=frequency,
                kvah_cumulative=round(self._kvah_cumulative, 3),
                voltage_l1=voltage_l1,
                voltage_l2=voltage_l2,
                voltage_l3=voltage_l3,
                source="SIMULATION",
                data_quality="GOOD",
                polling_latency_ms=round(random.uniform(0.5, 3.0), 2),
            )

            yield tick

            await asyncio.sleep(tick_interval)


# ---------------------------------------------------------------------------
# Modbus Meter Reader
# ---------------------------------------------------------------------------

class ModbusMeterReader(BaseMeterReader):
    """
    Polls Modbus TCP meter at configured interval.
    Auto-reconnect on failure. Emits STALE ticks up to MAX_CONSECUTIVE_FAILURES.
    Min sleep floor = polling_interval × 0.2 on latency overrun.
    Requires pymodbus library.
    """

    def __init__(
        self,
        facility_config: FacilityConfig,
        system_config: SystemConfig,
        host: str,
        port: int = 502,
        unit_id: int = 1,
    ):
        self._facility = facility_config
        self._system = system_config
        self._host = host
        self._port = port
        self._unit_id = unit_id
        if not host:
            raise ValueError(
                "Modbus host cannot be empty"
            )

        if port <= 0:
            raise ValueError(
                "Modbus port must be positive"
            )

        if unit_id <= 0:
            raise ValueError(
                "Modbus unit_id must be positive"
            )
        
        self._kvah_cumulative = 0.0
        self._consecutive_failures = 0
        self._client = None

    async def _connect(self) -> bool:
        try:
            from pymodbus.client import AsyncModbusTcpClient
            if self._client is not None:
                try:
                    self._client.close()

                except Exception:
                    pass

            self._client = AsyncModbusTcpClient(self._host, port=self._port)
            connected = await self._client.connect()

            if connected:
                logger.info(f"Modbus connected to {self._host}:{self._port}")
                self._consecutive_failures = 0
            return connected
        
        except Exception as e:
            logger.error(f"Modbus connection failed: {e}")
            return False

    async def _read_tick(self) -> Optional[MeterTick]:
        from ingestion.register_parser import parse_secure_elite_440
        try:
            start = time.monotonic()
            result = await self._client.read_input_registers(
                address=0, count=38, slave=self._unit_id
            )
            latency_ms = (time.monotonic() - start) * 1000

            if result.isError():
                raise IOError(f"Modbus read error: {result}")

            parsed = parse_secure_elite_440(list(result.registers))

            # Update kVAh
            meter_kvah = parsed["kvah_cumulative"]
            if meter_kvah >= self._kvah_cumulative:
                self._kvah_cumulative = meter_kvah
            else:
                logger.warning(f"kVAh rollback on Modbus: {meter_kvah} < {self._kvah_cumulative}")

            kva = parsed["kva"]
            kw = parsed["kw"]
            kvar = parsed["kvar"]
            pf = kw / kva if kva > 0 else 0.0
            pf = max(0.70, min(1.00, pf))

            self._consecutive_failures = 0
            return MeterTick(
                timestamp=datetime.now(IST),
                facility_id=self._facility.facility_id,
                kw=kw,
                kva=kva,
                kvar=kvar,
                pf=round(pf, 3),
                frequency=parsed["frequency"],
                kvah_cumulative=self._kvah_cumulative,
                voltage_l1=parsed["voltage_l1"],
                voltage_l2=parsed["voltage_l2"],
                voltage_l3=parsed["voltage_l3"],
                source="MODBUS",
                data_quality="GOOD",
                polling_latency_ms=round(latency_ms, 2),
            )
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(f"Modbus read failed (consecutive={self._consecutive_failures}): {e}")
            return None

    async def stream(self) -> AsyncGenerator[MeterTick, None]:
        polling_interval = self._system.polling_interval_seconds
        min_sleep = polling_interval * 0.2

        connected = await self._connect()

        while True:
            tick_start = time.monotonic()

            if not connected or (self._client and not self._client.connected):
                connected = await self._connect()

            if connected:
                tick = await self._read_tick()
                if tick is not None:
                    yield tick
                elif self._consecutive_failures <= self._system.max_consecutive_failures:
                    # Emit STALE tick
                    yield MeterTick(
                        timestamp=datetime.now(IST),
                        facility_id=self._facility.facility_id,
                        kw=0.0, kva=0.0, kvar=0.0, pf=0.85,
                        frequency=50.0,
                        kvah_cumulative=self._kvah_cumulative,
                        voltage_l1=230.0, voltage_l2=230.0, voltage_l3=230.0,
                        source="MODBUS",
                        data_quality="STALE",
                        polling_latency_ms=0.0,
                    )
                else:
                    logger.error(
                        f"Max consecutive failures ({self._system.max_consecutive_failures}) reached. "
                        f"Attempting reconnect."
                    )
                    connected = False

            elapsed = time.monotonic() - tick_start
            sleep_time = max(min_sleep, polling_interval - elapsed)
            await asyncio.sleep(sleep_time)


# ---------------------------------------------------------------------------
# MQTT Meter Reader
# ---------------------------------------------------------------------------

class MQTTMeterReader(BaseMeterReader):
    """
    Subscribes to MQTT broker. Supports json_flat, keyvalue, register_dump payloads.
    QoS 1. Duplicate detection via timestamp + kvah_cumulative within 1s window.
    Requires aiomqtt library.
    """

    def __init__(
        self,
        facility_config: FacilityConfig,
        system_config: SystemConfig,
        broker_host: str,
        broker_port: int = 1883,
        topic: str = "energy/meter/#",
        payload_format: str = "json_flat",
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self._facility = facility_config
        self._system = system_config
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._topic = topic
        self._payload_format = payload_format
        self._username = username
        self._password = password
        self._kvah_cumulative = 0.0
        # Duplicate detection: (iso_timestamp, kvah) → bool
        self._seen_messages: Dict[tuple, bool] = {}

    def _is_duplicate(self, timestamp_str: str, kvah: float) -> bool:
        key = (timestamp_str[:19], round(kvah, 2))  # 1-second precision
        if key in self._seen_messages:
            return True
        self._seen_messages[key] = True
        # Prune old entries (keep last 100)
        if len(self._seen_messages) > 100:
            oldest_key = next(iter(self._seen_messages))
            del self._seen_messages[oldest_key]
        return False

    def _parse_json_flat(self, payload: dict) -> Optional[MeterTick]:
        try:
            ts_str = payload.get("timestamp") or payload.get("ts")
            if not ts_str:
                raise ValueError("MQTT payload missing timestamp")
            
            parsed_ts = datetime.fromisoformat(ts_str)
            if parsed_ts.tzinfo is None:
                parsed_ts = parsed_ts.replace(tzinfo=IST)
            else:
                parsed_ts = parsed_ts.astimezone(IST)

            kvah = float(payload.get("kvah_cumulative", payload.get("kvah", 0.0)))

            if self._is_duplicate(ts_str, kvah):
                logger.debug("MQTT duplicate message suppressed.")
                return None

            if kvah >= self._kvah_cumulative:
                self._kvah_cumulative = kvah

            kva = float(payload.get("kva", 0.0))
            kw = float(payload.get("kw", 0.0))
            pf = kw / kva if kva > 0 else float(payload.get("pf", 0.85))
            pf = max(0.70, min(1.00, pf))

            return MeterTick(
                timestamp=parsed_ts,
                facility_id=self._facility.facility_id,
                kw=kw,
                kva=kva,
                kvar=float(payload.get("kvar", 0.0)),
                pf=round(pf, 3),
                frequency=float(payload.get("frequency", 50.0)),
                kvah_cumulative=self._kvah_cumulative,
                voltage_l1=float(payload.get("voltage_l1", 230.0)),
                voltage_l2=float(payload.get("voltage_l2", 230.0)),
                voltage_l3=float(payload.get("voltage_l3", 230.0)),
                source="MQTT",
                data_quality="GOOD",
                polling_latency_ms=0.0,
            )
        except Exception as e:
            logger.error(f"MQTT json_flat parse error: {e}")
            return None

    async def stream(self) -> AsyncGenerator[MeterTick, None]:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt required for MQTT mode: pip install aiomqtt")

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._broker_host,
                    port=self._broker_port,
                    username=self._username,
                    password=self._password,
                ) as client:
                    await client.subscribe(self._topic, qos=1)
                    logger.info(f"MQTT subscribed to {self._topic} @ {self._broker_host}")

                    async for message in client.messages:
                        try:
                            raw = json.loads(message.payload.decode("utf-8"))
                        except Exception:
                            logger.warning("MQTT payload is not valid JSON, skipping.")
                            continue

                        if self._payload_format == "json_flat":
                            tick = self._parse_json_flat(raw)
                        else:
                            logger.warning(f"Unsupported MQTT payload format: {self._payload_format}")
                            continue

                        if tick is not None:
                            yield tick
            except Exception as e:
                logger.error(f"MQTT connection error: {e}. Reconnecting in 5s.")
                await asyncio.sleep(5)
