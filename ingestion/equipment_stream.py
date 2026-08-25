from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Optional

from core.config import IST
from decision.equipment_state_manager import (
    EquipmentStateManager,
    EquipmentSource,
    EquipmentQuality,
)

logger = logging.getLogger(__name__)


class MQTTEquipmentReader:
    """
    Receives equipment ON/OFF telemetry from MQTT
    and updates EquipmentStateManager.

    Example payload:

    {
        "load_id": "load_004",
        "is_running": true,
        "timestamp": "2026-05-23T13:04:00+05:30"
    }
    """

    def __init__(
        self,
        equipment_manager: EquipmentStateManager,
        broker_host: str,
        broker_port: int = 1883,
        topic: str = "energy/equipment/#",
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):

        self._equipment_manager = equipment_manager

        self._broker_host = broker_host
        self._broker_port = broker_port

        self._topic = topic

        self._username = username
        self._password = password

    async def stream(self) -> AsyncGenerator[None, None]:

        try:
            import aiomqtt

        except ImportError:
            raise ImportError(
                "aiomqtt required: pip install aiomqtt"
            )

        while True:

            try:

                async with aiomqtt.Client(
                    hostname=self._broker_host,
                    port=self._broker_port,
                    username=self._username,
                    password=self._password,
                ) as client:

                    await client.subscribe(
                        self._topic,
                        qos=1,
                    )

                    logger.info(
                        "Equipment MQTT subscribed to %s",
                        self._topic,
                    )

                    async for message in client.messages:

                        try:

                            payload = json.loads(
                                message.payload.decode("utf-8")
                            )

                            load_id = payload["load_id"]

                            raw_running = payload["is_running"]

                            if isinstance(raw_running, bool):
                                is_running = raw_running

                            elif isinstance(raw_running, str):

                                normalized = raw_running.strip().lower()

                                if normalized in {"true", "1", "on"}:
                                    is_running = True

                                elif normalized in {"false", "0", "off"}:
                                    is_running = False

                                else:
                                    raise ValueError(
                                        f"Invalid is_running value: {raw_running}"
                                    )

                            else:
                                raise ValueError(
                                    f"Invalid is_running type: {type(raw_running)}"
                                )

                            timestamp_raw = payload.get(
                                "timestamp"
                            )

                            if timestamp_raw:
                                timestamp = (
                                    datetime
                                    .fromisoformat(timestamp_raw)
                                )

                                if timestamp.tzinfo is None:

                                    logger.warning(
                                        "Equipment timestamp missing timezone. Assuming IST."
                                    )

                                    timestamp = timestamp.replace(
                                        tzinfo=IST
                                    )

                            else:
                                timestamp = datetime.now(IST)

                            self._equipment_manager.update_state(
                                load_id=load_id,
                                is_running=is_running,
                                timestamp=timestamp,
                                source=EquipmentSource.MQTT,
                                quality=EquipmentQuality.GOOD,
                            )

                            logger.debug(
                                "Equipment update: %s running=%s",
                                load_id,
                                is_running,
                            )

                            yield None

                        except Exception as e:

                            logger.warning(
                                "Equipment payload parse failed: %s",
                                e,
                            )

            except Exception as e:

                logger.error(
                    "Equipment MQTT connection error: %s",
                    e,
                )

                await asyncio.sleep(7)