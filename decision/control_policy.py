import logging
import time
from dataclasses import dataclass
from enum import Enum

from optimization.constraint_engine import ConstraintEngine

logger = logging.getLogger(__name__)


# -------------------------
# Enums
# -------------------------
class RiskLevel(Enum):
    SAFE = 0
    WARNING = 1
    CRITICAL = 2


# -------------------------
# Output Contract
# -------------------------
@dataclass
class ControlDecision:
    required_reduction_kva: float
    is_feasible: bool
    should_act: bool
    should_escalate: bool
    reason: str


# -------------------------
# Control Policy
# -------------------------
class ControlPolicy:
    """
    Deterministic control layer.
    Handles:
    - demand targeting
    - feasibility awareness
    - actuator stability (cooldown + hysteresis)
    - escalation logic

    Does NOT:
    - select loads
    - optimize plans
    """

    def __init__(
        self,
        constraint_engine,
        safety_margin_kva: float = 50.0,
        min_action_kva: float = 5.0,
        cooldown_sec: int = 60,
        hysteresis_kva: float = 15.0,
        max_invalid_input_kva: float = 10000.0,
    ):
        self._constraint_engine: ConstraintEngine = constraint_engine

        # tuning parameters
        self._safety_margin_kva = safety_margin_kva
        self._min_action_kva = min_action_kva
        self._cooldown_sec = cooldown_sec
        self._hysteresis_kva = hysteresis_kva
        self._max_invalid_input_kva = max_invalid_input_kva

        # control memory
        self._last_action_ts = 0.0
        self._last_target_kva = None
        self._last_required_reduction = 0.0

    # -------------------------
    # Public API
    # -------------------------
    def evaluate(self, risk_state) -> ControlDecision:

        now = time.time()

        # -------------------------
        # STEP 0 - Input validation (CRITICAL FIX)
        # -------------------------
        current_mdi = getattr(risk_state, "projected_MDI_kva", None)
        contract = getattr(risk_state, "contract_demand_kva", None)

        if (
            current_mdi is None
            or contract is None
            or current_mdi < 0
            or contract <= 0
            or current_mdi > self._max_invalid_input_kva
        ):
            logger.error(
                "[CONTROL][INVALID_INPUT] mdi=%s contract=%s",
                current_mdi,
                contract,
            )
            return self._no_action("INVALID_INPUT")

        try:
            risk_level = RiskLevel[risk_state.risk_level]
        except Exception:
            logger.error("[CONTROL][INVALID_RISK_LEVEL] %s", risk_state.risk_level)
            return self._no_action("INVALID_RISK")

        # -------------------------
        # STEP 1 - Safe target (FIXED hysteresis)
        # -------------------------
        base_target = max(0.0, contract - self._safety_margin_kva)

        if self._last_target_kva is None:
            safe_target = base_target
        else:
            # symmetric hysteresis band
            upper = base_target + self._hysteresis_kva
            lower = base_target - self._hysteresis_kva
            safe_target = min(max(self._last_target_kva, lower), upper)

        # -------------------------
        # STEP 2 - Required reduction
        # -------------------------
        required_reduction = max(0.0, current_mdi - safe_target)

        # -------------------------
        # STEP 3 - Deadband (non-critical only)
        # -------------------------
        if (
            required_reduction < self._min_action_kva
            and risk_level != RiskLevel.CRITICAL
        ):
            return self._no_action("DEADBAND")

        # -------------------------
        # STEP 4 - Cooldown (critical bypass)
        # -------------------------
        if (
            now - self._last_action_ts < self._cooldown_sec
            and risk_level != RiskLevel.CRITICAL
        ):
            return self._no_action("COOLDOWN")

        # -------------------------
        # STEP 5 - Feasibility (capacity only)
        # -------------------------
        loads = self._constraint_engine.get_available_loads()

        max_possible_reduction = sum(
            max(0.0, getattr(l, "typical_kva", 0.0))
            for l in loads
        )

        is_feasible = max_possible_reduction >= required_reduction

        # -------------------------
        # STEP 6 - Decision logic (FIXED WARNING behavior)
        # -------------------------
        should_act = False
        should_escalate = False
        reason = "NO_ACTION"

        if risk_level == RiskLevel.CRITICAL:

            if required_reduction <= 0:
                return self._no_action("CRITICAL_NO_REDUCTION_NEEDED")

            should_act = True
            if not is_feasible:
                should_escalate = True
                reason = "CRITICAL_ESCALATION_INSUFFICIENT_CAPACITY"
            else:
                reason = "CRITICAL_CONTROL"

        elif risk_level == RiskLevel.WARNING:
            if is_feasible and required_reduction > 0:
                should_act = True
                reason = "PREVENTIVE_CONTROL"
            elif not is_feasible:
                should_escalate = True
                reason = "WARNING_ESCALATION"

        else:
            return self._no_action("SAFE")

        # -------------------------
        # STEP 7 - Control memory (FIXED stability)
        # -------------------------
        if should_act:
            self._last_action_ts = now
            self._last_target_kva = safe_target
            self._last_required_reduction = required_reduction
        else:
            # decay memory to avoid stale locking
            self._last_required_reduction *= 0.9

        # -------------------------
        # STEP 8 - Logging (ENHANCED)
        # -------------------------
        logger.debug(
            "[CONTROL] mdi=%.1f target=%.1f required=%.1f max_cap=%.1f feasible=%s act=%s escalate=%s reason=%s",
            current_mdi,
            safe_target,
            required_reduction,
            max_possible_reduction,
            is_feasible,
            should_act,
            should_escalate,
            reason,
        )

        logger.debug(
            "[CONTROL_TRACE] projected=%.1f target=%.1f required=%.1f risk=%s",
            current_mdi,
            safe_target,
            required_reduction,
            risk_level.name,
        )

        return ControlDecision(
            required_reduction_kva=required_reduction,
            is_feasible=is_feasible,
            should_act=should_act,
            should_escalate=should_escalate,
            reason=reason,
        )

    # -------------------------
    # Helpers
    # -------------------------
    def _no_action(self, reason: str) -> ControlDecision:
        return ControlDecision(
            required_reduction_kva=0.0,
            is_feasible=True,
            should_act=False,
            should_escalate=False,
            reason=reason,
        )