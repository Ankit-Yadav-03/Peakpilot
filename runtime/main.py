"""
runtime/main.py
Main entrypoint. Bootstraps pipeline and FastAPI server concurrently.
Usage:
    python -m runtime.main [--mode simulation|modbus|mqtt] [--billing-day N] [--no-api]
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
import time
from outcome.outcome_worker import OutcomeWorker
from outcome.outcome_engine import OutcomeEngine
import threading

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_facility_config, load_tariff_config, load_system_config
from ingestion.meter_stream import SimulatedMeterReader, ModbusMeterReader
from runtime.pipeline import Pipeline
from api.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="Peakpilot - Industrial Decision Engine")
    parser.add_argument(
        "--mode",
        choices=["simulation", "modbus", "mqtt"],
        default="simulation",
        help="Ingestion mode (default: simulation)",
    )
    parser.add_argument("--billing-day", type=int, default=2, help="Billing cycle day (1-30)")
    parser.add_argument("--no-api", action="store_true", help="Disable API server")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--modbus-host", default="192.168.1.100", help="Modbus TCP host")
    parser.add_argument("--modbus-port", type=int, default=502, help="Modbus TCP port")
    parser.add_argument("--modbus-unit", type=int, default=1, help="Modbus unit ID")
    return parser.parse_args()


async def run_api(app, host: str, port: int):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def run_pipeline(pipeline: Pipeline):
    await pipeline.run()


async def main():
    args = parse_args()

    if not 1 <= args.billing_day <= 30:
        raise ValueError(
            "billing-day must be between 1 and 30"
        )

    # Change to project root so relative paths work
    os.chdir(PROJECT_ROOT)

    logger.info("Loading configuration...")
    system_config = load_system_config(args.config)
    tariff_config = load_tariff_config("data/tariffs.json")
    facility_config = load_facility_config("data/loads.json")

    logger.info(
        f"Facility: {facility_config.facility_name} | "
        f"Contract: {facility_config.contract_demand_kva} kVA | "
        f"DISCOM: {facility_config.discom} | "
        f"Mode: {args.mode}"
    )

    # Build meter reader
    if args.mode == "simulation":
        meter_reader = SimulatedMeterReader(
            facility_config=facility_config,
            system_config=system_config,
            billing_cycle_day=args.billing_day,
        )
    elif args.mode == "modbus":
        meter_reader = ModbusMeterReader(
            facility_config=facility_config,
            system_config=system_config,
            host=args.modbus_host,
            port=args.modbus_port,
            unit_id=args.modbus_unit,
        )
    else:
        raise ValueError(f"Mode '{args.mode}' not yet implemented in this entrypoint.")


    pipeline = Pipeline(
        facility_config=facility_config,
        tariff_config=tariff_config,
        system_config=system_config,
        schema_path="db/schema.sql",
        meter_reader=meter_reader,
        billing_cycle_day=args.billing_day,
    )

    # 🔴 Outcome Engine + Worker
    outcome_engine = OutcomeEngine(
        tariff_config,
        facility_config
    )

    outcome_worker = OutcomeWorker(
        system_config.db_path,
        outcome_engine,
        pipeline.event_logger,
        pipeline.confidence_engine,
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
        daemon=True
    )

    worker_thread.start()

    logger.info("OutcomeWorker started in background thread")

    if args.no_api:
        logger.info("API disabled. Running pipeline only.")
        await run_pipeline(pipeline)
    else:
        app = create_app(
            pipeline=pipeline,
            system_config=system_config,
            db_path=system_config.db_path,
        )
        host = system_config.api_host
        port = system_config.api_port
        logger.info(f"API server starting on http://{host}:{port}")
        logger.info(f"Dashboard: http://localhost:{port}/ui")
        logger.info(f"API docs:  http://localhost:{port}/docs")

        await asyncio.gather(
            run_pipeline(pipeline),
            run_api(app, host, port),
        )


if __name__ == "__main__":
    asyncio.run(main())
