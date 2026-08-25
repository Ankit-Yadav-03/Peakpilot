"""
runtime/pipeline.py
Main pipeline runner: streams ticks from ingestion source → DecisionEngine → console/API.
Manages async context, WAL flush worker, nightly calibration scheduling.
"""
from __future__ import annotations
import asyncio
import logging
from typing import  Callable, Optional

from core.config import IST, FacilityConfig, TariffConfig, SystemConfig
from core.models import PipelineResult
from decision.decision_engine import DecisionEngine
from ingestion.meter_stream import SimulatedMeterReader, BaseMeterReader
from decision.equipment_state_manager import EquipmentStateManager
from ingestion.equipment_stream import MQTTEquipmentReader
from learning.event_logger import DurableEventLogger
from intelligence.confidence_scorer import ConfidenceScorer

logger = logging.getLogger(__name__)


class Pipeline:
    """
    Top-level runtime orchestrator.
    Instantiates all components, runs tick loop, manages WAL flush and calibration.
    """

    def __init__(
        self,
        facility_config: FacilityConfig,
        tariff_config: TariffConfig,
        system_config: SystemConfig,
        schema_path: str,
        meter_reader: Optional[BaseMeterReader] = None,
        billing_cycle_day: int = 2,
        on_result: Optional[Callable[[PipelineResult], None]] = None,
    ):
        self._facility = facility_config
        self._tariff = tariff_config
        self._system = system_config
        self._on_result = on_result
        self._billing_cycle_day = billing_cycle_day

        self._equipment_manager = EquipmentStateManager()

        # Event logger
        self._event_logger = DurableEventLogger(
            db_path=system_config.db_path,
            schema_path=schema_path,
        )

        # Confidence engine (for outcome worker)
        self._confidence_engine = ConfidenceScorer()

        # Decision engine
        self._decision_engine = DecisionEngine(
            facility_config=facility_config,
            tariff_config=tariff_config,
            system_config=system_config,
            event_logger=self._event_logger,
            confidence_engine=self._confidence_engine,
            billing_cycle_day=billing_cycle_day,
            equipment_manager=self._equipment_manager,
        )

        # Meter reader (default: simulation)
        self._reader = meter_reader or SimulatedMeterReader(
            facility_config=facility_config,
            system_config=system_config,
            billing_cycle_day=billing_cycle_day,
        )

        self._equipment_reader = MQTTEquipmentReader(
            equipment_manager=self._equipment_manager,
            broker_host=system_config.mqtt_host,
            broker_port=system_config.mqtt_port,
            topic=system_config.mqtt_topic,
        )

        # Latest result for API access
        self._latest_result: Optional[PipelineResult] = None
        self._tick_count: int = 0
        self._results_buffer: list = []
        self._max_buffer: int = 500
        self._running = False

    @property
    def latest_result(self) -> Optional[PipelineResult]:
        return self._latest_result

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def results_buffer(self) -> list:
        return self._results_buffer
    
    @property
    def event_logger(self):
        return self._event_logger
    
    @property
    def confidence_engine(self):
        return self._confidence_engine
    
    async def _run_equipment_stream(self) -> None:

        while self._running:

            try:

                async for _ in self._equipment_reader.stream():
                    pass

            except asyncio.CancelledError:
                raise

            except ImportError:
                logger.error(
                    "Equipment telemetry disabled. "
                    "aiomqtt not installed."
                )

                return

            except Exception:

                logger.exception(
                    "Equipment telemetry stream crashed. Restarting in 5 seconds."
                )

                await asyncio.sleep(5)

    async def run(self) -> None:
        """Main run loop."""
        self._running = True

        # WAL crash recovery
        unflushed = self._event_logger.replay_wal()
        if unflushed:
            logger.warning(f"Replayed {len(unflushed)} unflushed WAL records on startup.")

        # Start WAL flush worker
        await self._event_logger.start_flush_worker()

        equipment_task = asyncio.create_task(
            self._run_equipment_stream()
        )

        logger.info(
            f"Pipeline started | Facility: {self._facility.facility_name} | "
            f"Contract: {self._facility.contract_demand_kva} kVA | "
            f"DISCOM: {self._facility.discom} | "
            f"Billing day: {self._billing_cycle_day}"
        )

        try:
            async for tick in self._reader.stream():
                if not self._running:
                    break

                result = await self._decision_engine.process_tick(tick)
                self._tick_count += 1
                self._latest_result = result

                # Buffer results for API
                self._results_buffer.append(result)
                if len(self._results_buffer) > self._max_buffer:
                    self._results_buffer.pop(0)

                # Console display
                self._display(result)

                # External callback (e.g., WebSocket broadcast)
                if self._on_result:
                    try:
                        self._on_result(result)
                    except Exception as e:
                        logger.error(f"on_result callback error: {e}")

        except asyncio.CancelledError:
            logger.info("Pipeline cancelled.")
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
        finally:

            self._running = False

            equipment_task.cancel()

            await asyncio.gather(
                equipment_task,
                return_exceptions=True,
            )

            await self._event_logger.stop_flush_worker()

            logger.info("Pipeline stopped.")

    def stop(self) -> None:
        self._running = False

    def _display(self, result: PipelineResult) -> None:
        """Console output per tick."""
        if not result.validation.valid:
            logger.warning(f"[REJECTED] tick errors: {result.validation.errors}")
            return

        tick = result.tick
        risk = result.risk_state
        rec = result.final_recommendation
        conf = result.confidence_result
        cost = result.cost_state

        if risk is None or rec is None:
            return

        # Only log every 10th tick at DEBUG, always log non-SAFE or non-NO_ACTION
        if risk.risk_level != "SAFE" or rec.recommendation_type != "NO_ACTION":
            logger.info(
                f"[{tick.timestamp.strftime('%H:%M:%S')}] "
                f"kVA={tick.kva:.1f} | MDI_proj={risk.projected_MDI_kva:.1f} | "
                f"Risk={risk.risk_level} | "
                f"Bill=₹{cost.projected_monthly_bill:,.0f} | "
                f"Conf={conf.score:.0%} | "
                f"Rec={rec.recommendation_type}"
            )
            if rec.recommendation_type not in ("NO_ACTION",) and not rec.suppressed:
                logger.info(f"\n{rec.message}\n")
        elif self._tick_count % 10 == 0:
            logger.debug(
                f"[{tick.timestamp.strftime('%H:%M:%S')}] "
                f"kVA={tick.kva:.1f} | MDI_proj={risk.projected_MDI_kva:.1f} | "
                f"SAFE | Bill=₹{cost.projected_monthly_bill:,.0f}"
            )