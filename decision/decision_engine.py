from __future__ import annotations
import asyncio
import logging
from datetime import timedelta
from typing import Optional
from collections import OrderedDict

from core.models import MeterTick, PipelineResult, WindowState, Recommendation
from core.config import FacilityConfig, TariffConfig, SystemConfig
from ingestion.payload_validator import PayloadValidator
from state.state_detector import StateDetector
from tariff.tariff_engine import TariffEngine
from cost.cost_simulator import CostSimulator
from risk.risk_estimator import RiskEstimator
from risk.anomaly_detector import AnomalyDetector
from optimization.constraint_engine import ConstraintEngine
from optimization.optimizer import TODOptimizer
from optimization.conflict_resolver import ConflictResolver
from intelligence.confidence_scorer import ConfidenceScorer
from decision.intelligent_planner import IntelligentPlanner
from decision.control_policy import ControlPolicy
from decision.action_state_manager import ActionStateManager
from decision.equipment_state_manager import EquipmentStateManager
from decision.execution_state_manager import (
    ExecutionCommand,
    ExecutionStateManager,
    ExecutionStatus,
)
from decision.recovery_engine import RecoveryEngine
from recommendation.recommendation_engine import RecommendationEngine
from learning.event_logger import DurableEventLogger

logger = logging.getLogger(__name__)


class DecisionEngine:
    
    MAX_RECOMMENDATION_CACHE = 10000
    DEFAULT_ACTION_DURATION_SEC = 120

    def __init__(
        self,
        facility_config: FacilityConfig,
        tariff_config: TariffConfig,
        system_config: SystemConfig,
        event_logger: DurableEventLogger,
        confidence_engine: ConfidenceScorer,
        equipment_manager: EquipmentStateManager,
        billing_cycle_day: int = 1,
    ):
        self._facility = facility_config
        self._tariff = tariff_config
        self._system = system_config
        self._billing_cycle_day = billing_cycle_day

        self._validator = PayloadValidator(facility_config)

        self._window_events: asyncio.Queue = asyncio.Queue()
        self._confidence_engine = confidence_engine

        self._state_detector = StateDetector(
            facility_config,
            system_config,
            on_window_complete=self._on_window_complete,
        )

        self._tariff_engine = TariffEngine(tariff_config, facility_config)
        self._cost_simulator = CostSimulator(tariff_config, facility_config, system_config)
        self._risk_estimator = RiskEstimator(facility_config, system_config)
        self._anomaly_detector = AnomalyDetector(facility_config, system_config)
        self._constraint_engine = ConstraintEngine(facility_config, tariff_config)
        self._planner = IntelligentPlanner(self._constraint_engine)
        self._control_policy = ControlPolicy(self._constraint_engine)
        self._tod_optimizer = TODOptimizer(facility_config, tariff_config)
        self._conflict_resolver = ConflictResolver()
        self._action_manager = ActionStateManager()
        self._equipment_manager = equipment_manager
        self._event_logger = event_logger
        self._execution_manager = ExecutionStateManager(
            event_logger=self._event_logger,
            equipment_manager=self._equipment_manager,
            equipment_freshness_sec=system_config.stale_data_threshold_seconds,
        )
        self._recommendation_engine = RecommendationEngine(facility_config, self._action_manager)
        self._recommendation_cache: OrderedDict[str, Recommendation] = OrderedDict()
        self._recovery_engine = RecoveryEngine()

        self._lock = asyncio.Lock()

        self._last_completed_window_mdi: float = 0.0
        self._last_recommendation_signature = None
        self._last_recommendation_time = None

    async def _on_window_complete(self, window_state: WindowState, mdi_kva: float) -> None:
        await self._window_events.put((window_state, mdi_kva))

    def set_billing_cycle_day(self, day: int) -> None:
        self._billing_cycle_day = day

    def get_recommendation(
        self,
        decision_id: str
    ) -> Optional[Recommendation]:

        return self._recommendation_cache.get(decision_id)

    def get_execution_status(self, decision_id: str) -> dict:
        return self._execution_manager.get_latest_status_by_decision(decision_id)

    async def process_tick(self, tick: MeterTick) -> PipelineResult:
        async with self._lock:
            return await self._process_tick_inner(tick)

    async def _process_tick_inner(self, tick: MeterTick) -> PipelineResult:
        pipeline_errors = []
        current_time = tick.timestamp
        action_duration_sec = self.DEFAULT_ACTION_DURATION_SEC
        re_evaluation_time = current_time + timedelta(seconds=action_duration_sec)

        self._action_manager.cleanup_expired(current_time)

        self._execution_manager.clear_terminal_records(
            now=current_time,
            retention_sec=3600,
        )

        while not self._window_events.empty():
            window_state_evt, mdi_kva = await self._window_events.get() 

            self._last_completed_window_mdi = mdi_kva
            self._risk_estimator.update_monthly_md(mdi_kva)
            self._anomaly_detector.on_window_complete(mdi_kva)

            self._event_logger.log_window_event(
                window_state=window_state_evt,
                actual_mdi=mdi_kva,
            )

            logger.info(f"[SYNC] Window processed before tick: MDI={mdi_kva:.1f} kVA")

        validation = self._validator.validate(tick)
        if not validation.valid:
            logger.warning(f"Tick rejected: {validation.errors}")
            return PipelineResult(
                tick=tick,
                validation=validation,
                window_state=None,
                tariff_state=None,
                cost_state=None,
                risk_state=None,
                anomaly_state=None,
                demand_recommendation=None,
                tod_recommendation=None,
                conflict_resolution=None,
                confidence_result=None,
                final_recommendation=None,
                pipeline_errors=validation.errors,
            )

        window_state = await self._state_detector.process_tick(tick)

        anomaly_state = self._anomaly_detector.detect(tick, window_state.window_start)

        tariff_state = self._tariff_engine.compute(tick)

        projected_mdi = self._state_detector.projected_mdi
        risk_state = self._risk_estimator.compute(
            tick=tick,
            window_state=window_state,
            anomaly_state=anomaly_state,
            projected_mdi_kva=projected_mdi,
            billing_cycle_day=self._billing_cycle_day,
        )

        cost_state = self._cost_simulator.compute(
            tick=tick,
            tariff_state=tariff_state,
            window_state=window_state,
            projected_mdi_kva=projected_mdi,
            months_md_so_far_kva=self._risk_estimator._monthly_md_kva,
        )

        confirmed_records = self._execution_manager.reconcile(tick)

        shed_confirmed = [
            record for record in confirmed_records
            if record.status == ExecutionStatus.SHED_CONFIRMED
        ]

        if shed_confirmed:

            grouped_records: dict[str, list] = {}

            for record in shed_confirmed:
                grouped_records.setdefault(
                    record.decision_id,
                    []
                ).append(record)

            for decision_id, records in grouped_records.items():

                self._action_manager.mark_shed_confirmed(
                    load_ids=[
                        record.load_id
                        for record in records
                    ],
                    duration_sec=action_duration_sec,
                    confirmed_at=current_time,
                    decision_id=decision_id,
                    execution_event_ids={
                        record.load_id: record.event_id
                        for record in records
                    },
                    cooldown_minutes=self._system.decision_cooldown_minutes,
                )

        restore_confirmed = [
            record for record in confirmed_records
            if record.status == ExecutionStatus.RESTORE_CONFIRMED
        ]

        if restore_confirmed:
            self._action_manager.mark_restore_confirmed(
                [record.load_id for record in restore_confirmed]
            )

        demand_rec = None
        planner_status = None
        force_no_action = False

        if not anomaly_state.inrush_suppression_active:

            control_decision = self._control_policy.evaluate(risk_state)

            logger.debug(
                "[CONTROL_FLOW] required=%.1f | feasible=%s | act=%s | escalate=%s",
                control_decision.required_reduction_kva,
                control_decision.is_feasible,
                control_decision.should_act,
                control_decision.should_escalate
            )

            required_reduction = control_decision.required_reduction_kva
            achieved = 0.0

            if not control_decision.should_act:
                planner_status = f"NO_ACTION_{control_decision.reason}"

            elif control_decision.should_escalate:
                planner_status = "ESCALATION"

                logger.error(
                    "[ESCALATION] required=%.1f kVA | reason=%s",
                    required_reduction,
                    control_decision.reason
                )

                pipeline_errors.append(
                    f"ESCALATION: required={required_reduction:.1f}"
                )

            else:

                if required_reduction < 5.0:
                    planner_status = "SKIPPED_LOW_REDUCTION"
                    achieved = 0.0

                    logger.info(
                        "[DECISION] required=%.1f kVA | skipped planner (low reduction)",
                        required_reduction
                    )

                else:
                    available_loads = self._constraint_engine.get_available_loads()
                    filtered_available_loads = []

                    for load in available_loads:

                        # Action manager validation
                        if not self._action_manager.can_activate(
                            load.load_id,
                            current_time,
                        ):
                            continue

                        # Equipment telemetry validation
                        is_running = self._equipment_manager.is_running(
                            load.load_id
                        )

                        # Telemetry-authoritative execution model:
                        # unknown telemetry cannot safely participate
                        # in automated control recommendations.

                        if is_running is None:

                            logger.warning(
                                "Skipping %s: telemetry unknown",
                                load.load_id,
                            )

                            continue

                        # Block already-OFF loads
                        if is_running is False:

                            logger.info(
                                "Skipping %s: equipment already OFF",
                                load.load_id,
                            )

                            continue

                        filtered_available_loads.append(load)

                    if not filtered_available_loads:
                        plan = None
                        status = "NO_ACTION_ALL_LOADS_BLOCKED"
                        planner_status = status
                        force_no_action = True
                    else:
                        plan, status = self._planner.generate_plan(
                            required_reduction_kva=required_reduction,
                            risk_state=risk_state,
                            available_loads_override=filtered_available_loads
                        )

                    if plan:
                        demand_rec = plan
                        achieved = plan.expected_reduction_kva
                        planner_status = status

                    else:
                        achieved = 0.0
                        planner_status = status or "NO_PLAN"

                        if (
                            risk_state.risk_level == "CRITICAL"
                            and status == "BELOW_MIN_EFFECT"
                        ):
                            logger.warning(
                                "[FORCED_ACTION] No optimal plan. Selecting best available loads."
                            )

                            fallback = self._constraint_engine.select_loads_to_shed(
                                required_reduction_kva=required_reduction,
                                remaining_minutes=risk_state.remaining_minutes,
                                risk_state=risk_state,
                                available_loads_override=filtered_available_loads,
                            )

                            if fallback:
                                filtered_fallback_loads = [
                                    lid for lid in fallback.recommended_loads
                                    if self._action_manager.can_activate(lid, current_time)
                                ]

                                if not filtered_fallback_loads:
                                    fallback = None
                                else:
                                    fallback.recommended_loads = filtered_fallback_loads

                            if fallback:
                                demand_rec = fallback
                                achieved = fallback.expected_reduction_kva
                                planner_status = "FORCED_PARTIAL"

                                logger.warning(
                                    "[FORCED_ACTION] achieved=%.1f kVA (partial)",
                                    achieved
                                )

                    if demand_rec and not demand_rec.recommended_loads:
                        demand_rec = None
                        achieved = 0.0
                        planner_status = "NO_ACTION_EMPTY_PLAN"
                        force_no_action = True

                    logger.info(
                        "[DECISION] required=%.1f kVA | achieved=%.1f kVA | status=%s",
                        required_reduction,
                        achieved,
                        planner_status
                    )

        elif anomaly_state.inrush_suppression_active:
            logger.debug("Inrush suppression active: demand recommendations suppressed this window.")
            pipeline_errors.append("INRUSH_SUPPRESSION: demand recommendations suppressed.")

        # 🔴 RECOVERY EVALUATION (ONLY IF NO SHED)
        recovery_loads = (
            self._recovery_engine.evaluate(
                action_manager=self._action_manager,
                equipment_manager=self._equipment_manager,
                risk_state=risk_state,
                current_time=current_time,
            )
            if demand_rec is None
            else None
        )

        tod_rec = self._tod_optimizer.compute(tick, tariff_state, risk_state)

        conflict_resolution = self._conflict_resolver.resolve(demand_rec, tod_rec, risk_state)

        confidence_result = self._confidence_engine.compute(risk_state, anomaly_state, tariff_state)

        final_recommendation = self._recommendation_engine.build(
            risk_state=risk_state,
            anomaly_state=anomaly_state,
            tariff_state=tariff_state,
            cost_state=cost_state,
            demand_rec=demand_rec,
            tod_rec=tod_rec,
            conflict_resolution=conflict_resolution,
            confidence_result=confidence_result,
        )

        DEDUP_WINDOW_SEC = 1800

        if (
            final_recommendation.recommendation_type
            in ("PRE_RUN", "DELAY")
            and not final_recommendation.suppressed
        ):

            signature = (
                final_recommendation.recommendation_type,
                tuple(sorted(final_recommendation.loads_selected)),
            )

            duplicate = (
                self._last_recommendation_signature == signature
                and self._last_recommendation_time is not None
                and (
                    current_time
                    - self._last_recommendation_time
                ).total_seconds()
                < DEDUP_WINDOW_SEC
            )

            if duplicate:

                logger.debug(
                    "[TOD_DEDUP] Suppressing duplicate TOD recommendation."
                )

                final_recommendation.recommendation_type = "NO_ACTION"
                final_recommendation.loads_selected = []
                final_recommendation.expected_mdi_reduction_kva = 0.0
                final_recommendation.economic_impact = None
                final_recommendation.trigger = "SCHEDULED"
                final_recommendation.message = (
                    "Duplicate TOD recommendation suppressed."
                )

            else:

                self._last_recommendation_signature = signature
                self._last_recommendation_time = current_time

        # 🔴 APPLY RECOVERY (ONLY IF NO SHED DECISION)
        if (
            demand_rec is None
            and recovery_loads
            and not final_recommendation.suppressed
        ):
            final_recommendation.recommendation_type = "RESTORE"
            final_recommendation.loads_selected = recovery_loads
            final_recommendation.expected_mdi_reduction_kva = 0.0
            final_recommendation.economic_impact = None
            final_recommendation.trigger = "SYSTEM"
            load_list = ", ".join(final_recommendation.loads_selected)
            final_recommendation.message = (
                "SAFE TO RESTORE:\n"
                f"Previously shed load(s) eligible for restoration: {load_list}\n"
                "Demand has stabilized below control threshold."
            )

        if force_no_action:
            final_recommendation.recommendation_type = "NO_ACTION"
            final_recommendation.loads_selected = []
            final_recommendation.expected_mdi_reduction_kva = 0.0
            final_recommendation.economic_impact = None
            final_recommendation.trigger = "SCHEDULED"
            final_recommendation.message = "No action: recommended loads are already active or in cooldown."

        if planner_status == "ESCALATION":
            if final_recommendation.meta is None:
                final_recommendation.meta = {}

            final_recommendation.meta["escalation"] = True
            final_recommendation.meta["escalation_reason"] = control_decision.reason

            final_recommendation.message = (
                "Insufficient controllable load. Manual intervention required."
            )

        if planner_status:
            if final_recommendation.meta is None:
                final_recommendation.meta = {}

            final_recommendation.meta["planner_status"] = planner_status

        if final_recommendation.meta is None:
            final_recommendation.meta = {}

        final_recommendation.meta["duration_sec"] = action_duration_sec
        final_recommendation.meta["re_evaluation_time"] = re_evaluation_time

        if (final_recommendation.recommendation_type in ("SHED", "RESTORE") and not final_recommendation.suppressed):

            self._event_logger.log_decision(
                recommendation=final_recommendation,
                risk_state=risk_state,
                tariff_state=tariff_state,
            )

        try:

            if (
                final_recommendation.recommendation_type == "SHED"
                and final_recommendation.economic_impact is None
            ):

                projected_mdi = risk_state.projected_MDI_kva

                contract = risk_state.contract_demand_kva

                potential_excess = max(
                    0.0,
                    projected_mdi - (contract * 0.90)
                )

                penalty_rate = (
                    self._tariff.demand_charge_per_kVA
                )

                projected_saving = (
                    potential_excess * penalty_rate
                )

                if projected_saving > 0:

                    from core.models import EconomicImpact

                    final_recommendation.economic_impact = (
                        EconomicImpact(
                            prevented_md_kva=(
                                final_recommendation.expected_mdi_reduction_kva
                            ),

                            projected_saving_rupees=round(
                                projected_saving,
                                2
                            ),

                            economic_status="FALLBACK_ESTIMATE",

                            saving_basis="PROJECTED_EXCESS_MD"
                        )
                    )

        except Exception:

            logger.exception(
                "Fallback economic impact calculation failed"
            )

        if (
            final_recommendation.recommendation_type == "SHED"
            and not final_recommendation.suppressed
            and final_recommendation.loads_selected
            and final_recommendation.decision_id
            not in self._recommendation_cache
        ):
            self._execution_manager.create_pending_for_recommendation(
                recommendation=final_recommendation,
                tick=tick,
                command_type=ExecutionCommand.SHED,
            )

        if (
            final_recommendation.recommendation_type == "RESTORE"
            and final_recommendation.loads_selected
            and final_recommendation.decision_id
            not in self._recommendation_cache
        ):
            restore_loads = []

            for load_id in final_recommendation.loads_selected:

                state = self._action_manager.get_state(load_id)

                if state is None:
                    continue

                if not self._action_manager.awaiting_restore(
                    load_id,
                    current_time,
                ):
                    continue

                if self._execution_manager.has_open_command(
                    load_id,
                    ExecutionCommand.RESTORE,
                ):
                    continue

                restore_loads.append(load_id)

            final_recommendation.loads_selected = restore_loads

            if not restore_loads:

                final_recommendation.recommendation_type = "NO_ACTION"
                final_recommendation.trigger = "SCHEDULED"
                final_recommendation.message = (
                    "No loads currently eligible for restoration."
                )

            if restore_loads:

                self._execution_manager.create_pending_for_recommendation(
                    recommendation=final_recommendation,
                    tick=tick,
                    command_type=ExecutionCommand.RESTORE,
                )
            
        final_recommendation.meta["active_loads"] = self._action_manager.get_active_loads(current_time)
        final_recommendation.meta["execution_status"] = (
            self._execution_manager.get_latest_status_by_decision(
                final_recommendation.decision_id
            )
        )

        self._recommendation_cache[
            final_recommendation.decision_id
        ] = final_recommendation

        while len(self._recommendation_cache) > self.MAX_RECOMMENDATION_CACHE:
            self._recommendation_cache.popitem(last=False)

        try:
            self._event_logger.log_telemetry(
                tick=tick,
                window_state=window_state,
                projected_mdi_kva=projected_mdi,
                tod_window=tariff_state.tod_window,
            )

        except Exception as e:
            logger.error(f"WAL write error (non-fatal): {e}")
            pipeline_errors.append(f"WAL_WRITE_ERROR: {e}")

        return PipelineResult(
            tick=tick,
            validation=validation,
            window_state=window_state,
            tariff_state=tariff_state,
            cost_state=cost_state,
            risk_state=risk_state,
            anomaly_state=anomaly_state,
            demand_recommendation=demand_rec,
            tod_recommendation=tod_rec,
            conflict_resolution=conflict_resolution,
            confidence_result=confidence_result,
            final_recommendation=final_recommendation,
            pipeline_errors=pipeline_errors,
        )
