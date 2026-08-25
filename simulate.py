"""
simulate.py
Standalone simulation runner. Tests full pipeline end-to-end on simulated meter stream.
Prints structured output. No API server required.
Usage:
    python simulate.py [--minutes N] [--billing-day N] [--verbose]
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

from outcome.outcome_engine import OutcomeEngine
from outcome.outcome_worker import OutcomeWorker
from equipment_simulator import SimulatedEquipmentTelemetry
from intelligence.confidence_scorer import ConfidenceScorer
from plant_model import SimulatedPlantModel

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_facility_config, load_tariff_config, load_system_config
from ingestion.meter_stream import SimulatedMeterReader
from decision.decision_engine import DecisionEngine
from decision.equipment_state_manager import EquipmentStateManager
from learning.event_logger import DurableEventLogger


def parse_args():
    p = argparse.ArgumentParser(description="Peakpilot Simulation Runner")
    p.add_argument("--minutes", type=int, default=20, help="Simulation duration (minutes)")
    p.add_argument("--billing-day", type=int, default=2, help="Billing cycle day")
    p.add_argument("--verbose", action="store_true", help="Print every tick")
    return p.parse_args()


async def run_simulation(minutes: int, billing_day: int, verbose: bool):
    os.chdir(PROJECT_ROOT)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("simulate")

    system_config = load_system_config("config.yaml")
    tariff_config = load_tariff_config("data/tariffs.json")
    facility_config = load_facility_config("data/loads.json")

    # Override duration
    system_config.simulation_duration_minutes = minutes
    system_config.simulation_tick_interval_seconds = 15    # normal sim

    event_logger = DurableEventLogger(
        db_path=system_config.db_path,
        schema_path="db/schema.sql",
    )

    # WAL replay and start flush worker
    unflushed = event_logger.replay_wal()
    if unflushed:
        logger.warning(f"Replayed {len(unflushed)} WAL records.")
    await event_logger.start_flush_worker()

    confidence_engine = ConfidenceScorer()
    equipment_manager = EquipmentStateManager()

    plant_model = SimulatedPlantModel(
        facility_config=facility_config,
        equipment_manager=equipment_manager,
    )

    equipment_sim = SimulatedEquipmentTelemetry(
        facility_config,
        equipment_manager,
    )

    equipment_sim.initialize()

    outcome_engine = OutcomeEngine(
        tariff_config,
        facility_config
    )

    outcome_worker = OutcomeWorker(
        system_config.db_path,
        outcome_engine,
        event_logger,
        confidence_engine,
    )

    def start_outcome_worker():

        while True:

            try:

                outcome_worker.run()

            except Exception:

                logger.exception(
                    "OutcomeWorker crashed. Restarting in 10 seconds."
                )

                time.sleep(10)


    worker_thread = threading.Thread(
        target=start_outcome_worker,
        daemon=True,
    )

    worker_thread.start()

    logger.info(
        "OutcomeWorker started in simulation runtime"
    )

    engine = DecisionEngine(
        facility_config=facility_config,
        tariff_config=tariff_config,
        system_config=system_config,
        event_logger=event_logger,
        confidence_engine=confidence_engine,
        equipment_manager=equipment_manager,
        billing_cycle_day=billing_day,
    )

    reader = SimulatedMeterReader(
        facility_config=facility_config,
        system_config=system_config,
        billing_cycle_day=billing_day,
        plant_model=plant_model,
    )

    print(f"\n{'='*70}")
    print(f"  Peakpilot - Simulation Runner")
    print(f"  Facility : {facility_config.facility_name}")
    print(f"  Contract : {facility_config.contract_demand_kva} kVA | DISCOM: {facility_config.discom}")
    print(f"  Duration : {minutes} minutes (simulated)")
    print(f"  Billing  : Day {billing_day}")
    print(f"{'='*70}\n")

    tick_count = 0
    rejected = 0
    decisions_made = 0
    max_mdi = 0.0
    total_saving = 0.0

    header = f"{'Time':>8} {'kVA':>7} {'PF':>5} {'MDI':>7} {'Risk':<9} {'TOD':<9} {'Rec':<10} {'Conf':>5} {'BillRs. ':>10}"
    print(header)
    print("-" * len(header))

    async for tick in reader.stream():
        result = await engine.process_tick(tick)
        tick_count += 1

        simulation_elapsed_minutes = (tick.timestamp - reader._start_time).total_seconds() / 60.0
        if simulation_elapsed_minutes >= minutes:
            break

        if not result.validation.valid:
            rejected += 1
            if verbose:
                print(f"  [REJECTED] {result.validation.errors}")
            continue

        risk = result.risk_state
        rec = result.final_recommendation

        if (
            rec
            and rec.recommendation_type == "SHED"
            and rec.loads_selected
        ):
            equipment_sim.shed(
                rec.loads_selected
            )

        elif (
            rec
            and rec.recommendation_type == "RESTORE"
            and rec.loads_selected
        ):
            equipment_sim.restore(
                rec.loads_selected
            )

        conf = result.confidence_result
        cost = result.cost_state
        tariff = result.tariff_state

        if risk and risk.projected_MDI_kva > max_mdi:
            max_mdi = risk.projected_MDI_kva

        if rec and rec.recommendation_type not in ("NO_ACTION",) and not rec.suppressed:
            decisions_made += 1

        if rec and rec.economic_impact:
            total_saving += rec.economic_impact.projected_saving_rupees


        # Print every tick if verbose, else only non-SAFE
        should_print = verbose or (risk and risk.risk_level != "SAFE") or (rec and rec.recommendation_type != "NO_ACTION")
        if should_print or tick_count % 20 == 0:
            risk_lvl = risk.risk_level if risk else "?"
            tod = tariff.tod_window if tariff else "?"
            mdi = f"{risk.projected_MDI_kva:.1f}" if risk else "?"
            rec_type = rec.recommendation_type if rec else "?"
            conf_val = f"{conf.score:.0%}" if conf else "?"
            bill = f"Rs. {cost.projected_monthly_bill:>8,.0f}" if cost else ""
            suppressed = "[SUP]" if (rec and rec.suppressed) else ""

            print(
                f"{tick.timestamp.strftime('%H:%M:%S'):>8} "
                f"{tick.kva:>7.1f} "
                f"{tick.pf:>5.3f} "
                f"{mdi:>7} "
                f"{risk_lvl:<9} "
                f"{tod:<9} "
                f"{rec_type:<10} "
                f"{conf_val:>5} "
                f"{bill:>10} {suppressed}"
            )

            if rec and rec.recommendation_type not in ("NO_ACTION",) and not rec.suppressed:
                print(f"\n  ┌─ RECOMMENDATION ────────────────────────────")
                for line in rec.message.split("\n"):
                    print(f"  │ {line}")
                print(f"  └─────────────────────────────────────────────\n")

    print(f"\n{'='*70}")
    print(f"  SIMULATION COMPLETE")
    print(f"  Total ticks                                : {tick_count}")
    print(f"  Rejected ticks                             : {rejected}")
    print(f"  Decisions made                             : {decisions_made}")
    print(f"  Max Projected MDI reached                  : {max_mdi:.1f} kVA")
    print(f"  Projected monthly avoided demand penalties : Rs. {total_saving:,.0f}")
    print(f"  DB written to                              : {system_config.db_path}")
    print(f"{'='*70}\n")

    await event_logger.stop_flush_worker()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_simulation(args.minutes, args.billing_day, args.verbose))
