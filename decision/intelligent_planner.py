import itertools
import logging
import time

from optimization.constraint_engine import ConstraintEngine

logger = logging.getLogger(__name__)


class IntelligentPlanner:

    MAX_ITERATIONS = 5000  # global cap (hard guard)

    def __init__(self, constraint_engine: ConstraintEngine):
        self._constraint_engine = constraint_engine

        # configurable weights (no magic numbers)
        # Rationale: achievement scaled to ~[0,100], disruption typically [1..5]
        # So weight ~[3..8] keeps disruption at ~5-40% influence.
        self._disruption_weight = getattr(
            self._constraint_engine, "disruption_weight", 5.0
        )

        # early-exit tuning (latency vs optimality)
        self._enable_early_exit = getattr(
            self._constraint_engine, "planner_early_exit", True
        )
        self._early_exit_margin = getattr(
            self._constraint_engine, "planner_early_exit_margin", 0.95
        )

        # acceptance threshold (no magic 0.6)
        self._min_ratio = getattr(
            self._constraint_engine, "min_acceptable_ratio", 0.60
        )

    # ---------------------------
    # scoring
    # ---------------------------
    def _score_plan(self, plan, required_reduction_kva):

        loads = getattr(plan, "recommended_loads", [])

        achieved = plan.expected_reduction_kva or 0.0

        if required_reduction_kva <= 0:
            return 0.0

        achievement_ratio = achieved / required_reduction_kva

        disruption_penalty = 0.0

        for l in loads:
            if isinstance(l, str):
                continue

            disruption_penalty += (
                (4 - l.priority) * 10
                + l.restart_penalty_minutes * 0.5
                + (5 if l.thermal_time_constant_minutes > 0 else 0)
            )

        return (
            achievement_ratio * 100.0
            - disruption_penalty * self._disruption_weight
        )

    # ---------------------------
    # budget allocation
    # ---------------------------
    def _per_r_budget(self, max_r):
        """
        Allocate budget across r-levels so higher-order combos aren't starved.
        Strategy: geometric weighting favoring larger r.
        """
        weights = [i + 1 for i in range(max_r)]  # [1,2,3,4,5]
        total_w = sum(weights)

        budgets = []
        for w in weights:
            b = int((w / total_w) * self.MAX_ITERATIONS)
            budgets.append(max(1, b))

        # fix rounding to match total
        diff = self.MAX_ITERATIONS - sum(budgets)
        idx = len(budgets) - 1
        while diff > 0:
            budgets[idx] += 1
            diff -= 1
        return budgets  # index 0 => r=1, ...

    # ---------------------------
    # main
    # ---------------------------
    def generate_plan(self, required_reduction_kva, risk_state, available_loads_override=None):
        start_time = time.time()

        if available_loads_override is not None:
            loads = available_loads_override
        else:
            loads = self._constraint_engine.get_available_loads()

        if not loads:
            return None, "NO_LOADS_AVAILABLE"

        # deterministic ordering: lower priority value = safer first
        loads = sorted(loads, key=lambda l: getattr(l, "priority", 5))

        best_plan = None
        best_score = float("-inf")
        best_partial = 0.0

        iteration_count = 0

        max_r = min(len(loads), 7)
        per_r_budget = self._per_r_budget(max_r)

        for r_idx, r in enumerate(range(1, max_r + 1)):
            budget_r = per_r_budget[r_idx]
            used_r = 0

            for combo in itertools.combinations(loads, r):
                iteration_count += 1
                used_r += 1

                if iteration_count >= self.MAX_ITERATIONS or used_r > budget_r:
                    break

                plan = self._constraint_engine.evaluate_load_combination(
                    combo,
                    required_reduction_kva,
                    risk_state
                )

                if not plan or not plan.is_valid:
                    continue

                achieved = plan.expected_reduction_kva or 0.0
                best_partial = max(best_partial, achieved)

                score = self._score_plan(plan, required_reduction_kva)

                prev_best_score = best_score

                if score > best_score:
                    best_score = score
                    best_plan = plan

                if (
                    self._enable_early_exit
                    and achieved >= required_reduction_kva
                    and prev_best_score > float("-inf")
                    and score >= (prev_best_score * self._early_exit_margin)
                    and score > prev_best_score
                ):
                    elapsed = (time.time() - start_time) * 1000
                    logger.debug(
                        "[PLANNER][EARLY_EXIT] r=%d iters=%d time=%.1fms achieved=%.1f/%.1f score=%.2f prev_best=%.2f",
                        r, iteration_count, elapsed,
                        achieved, required_reduction_kva, score, prev_best_score
                    )
                    return plan, None

            if iteration_count >= self.MAX_ITERATIONS:
                break

        elapsed_ms = (time.time() - start_time) * 1000

        logger.debug(
            "[PLANNER] iters=%d time=%.1fms best_partial=%.1f/%.1f best_score=%.2f",
            iteration_count,
            elapsed_ms,
            best_partial,
            required_reduction_kva,
            best_score
        )

        if not best_plan:
            return None, "NO_PLAN"

        achieved = best_plan.expected_reduction_kva or 0.0

        if achieved >= required_reduction_kva:
            return best_plan, None

        if achieved >= (self._min_ratio * required_reduction_kva):
            return best_plan, "PARTIAL"

        if achieved < 5.0:
            return None, "BELOW_MIN_EFFECT"

        return best_plan, "INSUFFICIENT_REDUCTION"