import json
import logging
import os
import random
import signal
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("industrialpulse.simulator")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
PUBLISH_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "5"))

_running = True


def _handle_signal(sig, frame):
    global _running
    logger.info("Shutdown signal received, stopping…")
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Base machine
# ---------------------------------------------------------------------------

class Machine:
    def __init__(self, machine_id: str) -> None:
        self.machine_id = machine_id
        self._fault_cycles_remaining = 0

    @property
    def in_fault(self) -> bool:
        return self._fault_cycles_remaining > 0

    def _tick_fault(self) -> None:
        if self.in_fault:
            self._fault_cycles_remaining -= 1
            if self._fault_cycles_remaining == 0:
                logger.info("%s: fault event recovered", self.machine_id)
        elif random.random() < 0.10:
            self._fault_cycles_remaining = random.randint(3, 8)
            logger.info(
                "%s: fault event triggered (%d cycles)",
                self.machine_id,
                self._fault_cycles_remaining,
            )

    def topic(self) -> str:
        return f"machines/{self.machine_id}/telemetry"

    def _sensors(self) -> dict:
        raise NotImplementedError

    def _status(self, sensors: dict) -> str:
        raise NotImplementedError

    def payload(self) -> dict:
        self._tick_fault()
        sensors = self._sensors()
        return {
            "machine_id": self.machine_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sensors": sensors,
            "status": self._status(sensors),
        }


# ---------------------------------------------------------------------------
# CNC Mill — cnc_01
# ---------------------------------------------------------------------------

class CNCMill(Machine):
    def __init__(self) -> None:
        super().__init__("cnc_01")
        self._temp = 70.0
        self._vib = 2.5
        self._rpm = 2000.0

    def _sensors(self) -> dict:
        if self.in_fault:
            self._temp = min(102.0, self._temp + random.uniform(2.0, 5.0))
            self._vib = min(14.0, self._vib + random.uniform(0.5, 2.0))
        else:
            self._temp = max(55.0, min(83.0, self._temp + random.uniform(-1.0, 1.0)))
            self._vib = max(0.1, min(6.5, self._vib + random.uniform(-0.3, 0.3)))

        self._rpm = max(900.0, min(3100.0, self._rpm + random.uniform(-60.0, 60.0)))

        return {
            "temperature": round(self._temp, 2),
            "vibration": round(self._vib, 3),
            "spindle_rpm": round(self._rpm, 1),
        }

    def _status(self, s: dict) -> str:
        if s["temperature"] > 95 or s["vibration"] > 10:
            return "critical"
        if s["temperature"] > 85 or s["vibration"] > 7:
            return "warning"
        return "normal"


# ---------------------------------------------------------------------------
# Hydraulic Press — press_02
# ---------------------------------------------------------------------------

class HydraulicPress(Machine):
    def __init__(self) -> None:
        super().__init__("press_02")
        self._pressure = 100.0
        self._oil_temp = 50.0
        self._cycles = 0

    def _sensors(self) -> dict:
        if self.in_fault:
            self._pressure = min(155.0, self._pressure + random.uniform(3.0, 8.0))
            self._oil_temp = min(82.0, self._oil_temp + random.uniform(1.0, 3.0))
        else:
            self._pressure = max(75.0, min(128.0, self._pressure + random.uniform(-2.0, 2.0)))
            self._oil_temp = max(38.0, min(63.0, self._oil_temp + random.uniform(-0.5, 0.5)))

        self._cycles += 1

        return {
            "pressure": round(self._pressure, 2),
            "oil_temperature": round(self._oil_temp, 2),
            "cycle_count": self._cycles,
        }

    def _status(self, s: dict) -> str:
        if s["pressure"] > 145 or s["oil_temperature"] > 75:
            return "critical"
        if s["pressure"] > 130 or s["oil_temperature"] > 65:
            return "warning"
        return "normal"


# ---------------------------------------------------------------------------
# Conveyor Belt — conveyor_03
# ---------------------------------------------------------------------------

class ConveyorBelt(Machine):
    def __init__(self) -> None:
        super().__init__("conveyor_03")
        self._speed = 1.2
        self._current = 10.0

    def _sensors(self) -> dict:
        if self.in_fault:
            if random.random() < 0.5:
                self._speed = max(0.0, self._speed - random.uniform(0.3, 0.8))
            else:
                self._speed = min(3.0, self._speed + random.uniform(0.3, 0.8))
            self._current = min(26.0, self._current + random.uniform(1.0, 4.0))
        else:
            self._speed = max(0.35, min(2.2, self._speed + random.uniform(-0.05, 0.05)))
            self._current = max(4.5, min(16.0, self._current + random.uniform(-0.5, 0.5)))

        items = max(0, int(self._speed * 20 + random.randint(-2, 2)))

        return {
            "belt_speed": round(self._speed, 3),
            "motor_current": round(self._current, 2),
            "items_per_minute": items,
        }

    def _status(self, s: dict) -> str:
        spd = s["belt_speed"]
        cur = s["motor_current"]
        if spd < 0.1 or spd > 2.5 or cur > 22:
            return "critical"
        if spd < 0.3 or spd > 2.3 or cur > 18:
            return "warning"
        return "normal"


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def _build_client() -> mqtt.Client:
    client = mqtt.Client(client_id="industrialpulse-simulator")

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker %s:%d", MQTT_HOST, MQTT_PORT)
        else:
            logger.error("MQTT connect failed, rc=%d", rc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect, rc=%d", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def _connect_with_retry(client: mqtt.Client, max_retries: int = 15) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            return True
        except Exception as exc:
            logger.warning("MQTT connect attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(5)
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    machines: list[Machine] = [CNCMill(), HydraulicPress(), ConveyorBelt()]

    client = _build_client()
    if not _connect_with_retry(client):
        logger.error("Could not connect to MQTT broker after retries — exiting")
        return

    client.loop_start()
    logger.info("Simulator running — publishing every %ds to %d machines", PUBLISH_INTERVAL, len(machines))

    while _running:
        for machine in machines:
            data = machine.payload()
            result = client.publish(machine.topic(), json.dumps(data), qos=1)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(
                    "%-15s status=%-8s temp/pressure/speed visible in payload",
                    machine.machine_id,
                    data["status"],
                )
            else:
                logger.warning("Publish failed for %s, rc=%d", machine.machine_id, result.rc)

        time.sleep(PUBLISH_INTERVAL)

    client.loop_stop()
    client.disconnect()
    logger.info("Simulator stopped cleanly")


if __name__ == "__main__":
    main()
