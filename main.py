# =============================================================================
# MAIN - Application Entry Point & Orchestration
# =============================================================================
# Wires all modules together and manages the application lifecycle.
#
# Each zone gets its own Zone instance which owns:
#   - Radiator physics
#   - Sensor-to-thermostat forwarding
#   - MPC scheduler
#
# Global services (weather, prices) are shared across all zones.
# =============================================================================

import logging
import signal
import sys
import threading

import config
from state import state
from infra.mqtt import mqtt
from infra.influx import influx
from interface.telegram_bot import create_bot
from interface import plot_server
from interface.ai_assistant.assistant import AIAssistant
from control.radiator import Radiator, supply_temp_from_outdoor
from forecasts import weather as weather_module
from forecasts import prices as prices_module
from forecasts import co2 as co2_module
from control import mpc as mpc_module

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: ZONE CLASS
# =============================================================================

class Zone:
    """
    Owns everything belonging to a single heating zone:
      - Radiator physics (EN 442)
      - Sensor-to-thermostat temperature forwarding
      - MPC scheduler
    """

    def __init__(self, zone_id: str, zone_cfg: dict):
        self.id           = zone_id
        self.display_name = zone_cfg["display_name"]
        self.devices      = zone_cfg["devices"]

        # Radiator — only if radiator config is present
        rad_cfg           = zone_cfg.get("radiator", {})
        # State key is always zone-scoped to avoid clashes between zones.
        # InfluxDB writes under "radiator_output" with zone tag (handled in influx.py).
        self.radiator_name = f"{zone_id}_radiator_output"
        rad_kwargs        = {k: v for k, v in rad_cfg.items() if k != "name"}
        self.radiator     = Radiator(**rad_kwargs) if rad_kwargs else None

        # MPC scheduler
        mpc_cfg         = zone_cfg.get("mpc", {})
        mpc_interval    = mpc_cfg.get("dt_minutes", 15) * 60
        mpc_enabled     = mpc_cfg.get("enabled", False)
        self.mpc_scheduler = MpcScheduler(self, mpc_interval, enabled=mpc_enabled)

        # Cache for thermostat forwarding — avoids spamming tiny changes
        self._coupling_last_sent: dict[str, float] = {}

        logger.info(f"Zone '{self.display_name}' ({self.id}) initialised")

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def thermostat_name(self) -> str | None:
        return self.devices.get("thermostat")

    @property
    def has_radiator_sensors(self) -> bool:
        return all(r in self.devices for r in ["supply_temp", "return_temp", "room_temp"])

    def get_device_name(self, role: str) -> str | None:
        return self.devices.get(role)

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self) -> None:
        from control import schedules as schedules_module
        schedules_module.ensure_room_exists(self.id)
        self._apply_thermostat_settings()
        state.on_device_update(self.on_sensor_update)
        self.mpc_scheduler.start()
        logger.info(f"Zone '{self.display_name}' started")

    def stop(self) -> None:
        self.mpc_scheduler.stop()

    # -------------------------------------------------------------------------
    # Sensor callbacks
    # -------------------------------------------------------------------------

    def on_sensor_update(self, name: str, payload: dict) -> None:
        """Called on every MQTT device update. Ignores devices not in this zone."""
        if name not in self.devices.values():
            return
        self._forward_to_thermostat(name, payload)
        self._update_radiator_output()

    def _forward_to_thermostat(self, name: str, payload: dict) -> None:
        """Forward room_temp sensor reading to thermostat as external sensor value."""
        if name != self.devices.get("room_temp"):
            return
        thermostat = self.thermostat_name
        if not thermostat:
            return
        temperature = payload.get("temperature")
        if temperature is None:
            return
        temperature = round(temperature, 1)
        last = self._coupling_last_sent.get(thermostat)
        if last is not None and abs(temperature - last) < 0.1:
            return   # suppress tiny fluctuations
        self._coupling_last_sent[thermostat] = temperature
        temp_centidegrees = int(temperature * 100)
        mqtt.send_command(thermostat, {"external_measured_room_sensor": temp_centidegrees})
        logger.info(f"[{self.display_name}] Forwarded {temperature}C -> {thermostat}")

    def _update_radiator_output(self) -> None:
        """Recalculate radiator heat output from current pipe temperatures."""
        if not self.radiator or not self.has_radiator_sensors:
            return
        t_room   = state.get_device(self.devices["room_temp"]).get("temperature")
        t_supply = state.get_device(self.devices["supply_temp"]).get("temperature")
        t_return = state.get_device(self.devices["return_temp"]).get("temperature")
        if None in [t_room, t_supply, t_return]:
            return
        watts = self.radiator.output(t_supply=t_supply, t_return=t_return, t_room=t_room)
        state.update_derived(self.radiator_name, {"watts": watts})

    def _apply_thermostat_settings(self) -> None:
        """Send default settings to thermostat on startup."""
        settings = config.ZONES[self.id].get("thermostat_settings", {})
        if settings and self.thermostat_name:
            mqtt.send_command(self.thermostat_name, settings)
            logger.info(f"Applied settings to {self.thermostat_name}")


# =============================================================================
# SECTION 2: BACKGROUND SCHEDULERS
# =============================================================================

class WeatherScheduler:
    """Periodically fetches weather data and updates state."""

    def __init__(self, interval_seconds: int):
        self.interval = interval_seconds
        self.timer    = None
        self.running  = False

    def start(self):
        self.running = True
        self._run_update()
        logger.info(f"Weather scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()

    def _run_update(self):
        if not self.running:
            return
        try:
            data = weather_module.fetch_current_weather()
            if data:
                state.update_weather(data)
        except Exception as e:
            logger.error(f"Weather update failed: {e}")
        if self.running:
            self.timer = threading.Timer(self.interval, self._run_update)
            self.timer.daemon = True
            self.timer.start()


class PriceScheduler:
    """Periodically fetches electricity prices and updates state."""

    def __init__(self, interval_seconds: int = 900):
        self.interval = interval_seconds
        self.timer    = None
        self.running  = False

    def start(self):
        self.running = True
        self._run_update()
        logger.info(f"Price scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()

    def _run_update(self):
        if not self.running:
            return
        try:
            data = prices_module.fetch_current_price()
            if data:
                state.update_price(data)
        except Exception as e:
            logger.error(f"Price update failed: {e}")
        if self.running:
            self.timer = threading.Timer(self.interval, self._run_update)
            self.timer.daemon = True
            self.timer.start()



class Co2Scheduler:
    """Periodically fetches CO2 emissions and updates state (every 5 min)."""

    def __init__(self, interval_seconds: int = 300):
        self.interval = interval_seconds
        self.timer    = None
        self.running  = False

    def start(self):
        self.running = True
        self._run_update()
        logger.info(f"CO2 scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()

    def _run_update(self):
        if not self.running:
            return
        try:
            data = co2_module.fetch_current_co2()
            if data:
                state.update_co2(data)
        except Exception as e:
            logger.error(f"CO2 update failed: {e}")
        if self.running:
            self.timer = threading.Timer(self.interval, self._run_update)
            self.timer.daemon = True
            self.timer.start()


class MpcScheduler:
    """
    Runs MPC optimization for one zone at clock-aligned intervals
    (:00, :15, :30, :45 for 15-minute steps).
    """

    def __init__(self, zone: Zone, interval_seconds: int, enabled: bool = True):
        self.zone     = zone
        self.interval = interval_seconds
        self.timer    = None
        self.running  = False
        self.enabled  = enabled

    def _seconds_until_next_slot(self) -> int:
        from datetime import datetime
        now              = datetime.now()
        interval_minutes = self.interval // 60
        next_aligned     = ((now.minute // interval_minutes) + 1) * interval_minutes
        if next_aligned >= 60:
            wait = (60 - now.minute) + (next_aligned - 60)
        else:
            wait = next_aligned - now.minute
        return max(5, wait * 60 - now.second + 5)

    def start(self):
        self.running = True
        delay        = self._seconds_until_next_slot()
        from datetime import datetime, timedelta
        next_run = datetime.now() + timedelta(seconds=delay)
        self.timer = threading.Timer(delay, self._run_update)
        self.timer.daemon = True
        self.timer.start()
        mode = "MPC" if self.enabled else "schedule control"
        logger.info(
            f"[{self.zone.display_name}] {mode} scheduler started "
            f"(first run at {next_run.strftime('%H:%M:%S')})"
        )

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()

    def _run_update(self):
        if not self.running:
            return
        try:
            if self.enabled:
                # --- MPC control ---
                weather   = state.get_weather()
                t_outdoor = weather.get("temp", 0.0) if weather else 0.0
                t_supply  = supply_temp_from_outdoor(t_outdoor)

                room_device = self.zone.get_device_name("room_temp")
                t_room = state.get_device(room_device).get("temperature") if room_device else None
                t_room = t_room or 20.0

                if self.zone.radiator:
                    max_heat = self.zone.radiator.max_output(t_supply=t_supply, t_room=t_room)
                else:
                    max_heat = 1000.0

                logger.info(
                    f"[{self.zone.display_name}] "
                    f"t_outdoor={t_outdoor:.1f}C -> t_supply={t_supply:.1f}C -> max_heat={max_heat:.0f}W"
                )

                setpoint = mpc_module.run_mpc_step(zone_id=self.zone.id, max_heat=max_heat)
                if setpoint is not None and self.zone.thermostat_name:
                    mqtt.send_command(
                        self.zone.thermostat_name,
                        {"occupied_heating_setpoint": setpoint}
                    )
                    logger.info(f"[{self.zone.display_name}] MPC setpoint sent: {setpoint:.1f}C")

            else:
                # --- Schedule control (MPC disabled) ---
                from control import schedules as schedules_module
                current = schedules_module.get_current_setpoint(room=self.zone.id)
                if current and self.zone.thermostat_name:
                    setpoint = current["T_min"]
                    mqtt.send_command(
                        self.zone.thermostat_name,
                        {"occupied_heating_setpoint": setpoint}
                    )
                    logger.info(
                        f"[{self.zone.display_name}] Schedule setpoint sent: "
                        f"{setpoint:.1f}C ({current['state']})"
                    )

        except Exception as e:
            logger.error(f"Scheduler update failed for zone '{self.zone.display_name}': {e}")

        if self.running:
            delay      = self._seconds_until_next_slot()
            self.timer = threading.Timer(delay, self._run_update)
            self.timer.daemon = True
            self.timer.start()


# =============================================================================
# SECTION 3: APPLICATION LIFECYCLE
# =============================================================================

class Application:
    """
    Orchestrates all components.

    Startup order:
        1. InfluxDB
        2. MQTT
        3. Zones (thermostat settings, sensor callbacks, MPC schedulers)
        4. Weather scheduler
        5. Price scheduler
        6. Telegram bot
    """

    def __init__(self):
        # Create one Zone instance per configured zone
        self.zones = {
            zone_id: Zone(zone_id, zone_cfg)
            for zone_id, zone_cfg in config.ZONES.items()
        }

        self.weather_scheduler = WeatherScheduler(config.WEATHER_UPDATE_INTERVAL)
        self.price_scheduler   = PriceScheduler(config.WEATHER_UPDATE_INTERVAL)
        self.co2_scheduler     = Co2Scheduler(interval_seconds=300)
        self.assistant         = AIAssistant(state)
        self.telegram_app      = None

    def start(self):
        logger.info("Starting TroubleHouseBot...")

        plot_server.start()
        influx.start()
        mqtt.start()

        for zone in self.zones.values():
            zone.start()

        self.weather_scheduler.start()
        self.price_scheduler.start()
        self.co2_scheduler.start()

        self.telegram_app = create_bot(assistant=self.assistant, zones=self.zones)

        logger.info("All services started")
        logger.info("Telegram bot running. Press Ctrl+C to stop.")

        self.telegram_app.run_polling()

    def stop(self):
        logger.info("Shutting down...")
        for zone in self.zones.values():
            zone.stop()
        self.weather_scheduler.stop()
        self.price_scheduler.stop()
        self.co2_scheduler.stop()
        mqtt.stop()
        influx.stop()
        logger.info("Goodbye!")


# =============================================================================
# SECTION 4: ENTRY POINT
# =============================================================================

def main():
    # Seed model parameter files for all zones if missing
    from control import model_store
    for zone_id in config.get_zone_ids():
        model_store.init_if_missing(zone_id)

    app = Application()

    def signal_handler(sig, frame):
        logger.info("Interrupt received...")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.start()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        app.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()