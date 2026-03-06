# =============================================================================
# MAIN - Application Entry Point & Orchestration
# =============================================================================
# This is the ONLY file that knows about all the other modules.
# It handles:
#   - Starting all services in the correct order
#   - Scheduling background tasks (weather updates)
#   - Graceful shutdown
#
# Run with: python main.py
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
from interface.ai_assistant.assistant import AIAssistant
from control.radiator import Radiator, supply_temp_from_outdoor
from forecasts import weather as weather_module
from forecasts import prices as prices_module
from control import mpc as mpc_module

# =============================================================================
# SECTION 1: LOGGING SETUP
# =============================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 2: THERMOSTAT INITIALIZATION
# =============================================================================

def _apply_thermostat_settings() -> None:
    """
    Send default settings to all configured thermostats.
    Called once on startup to ensure correct configuration.
    """
    settings_dict = getattr(config, 'THERMOSTAT_DEFAULT_SETTINGS', {})
    
    for thermostat, settings in settings_dict.items():
        if not settings:
            continue
        mqtt.send_command(thermostat, settings)
        logger.info(f"⚙️ Applied settings to {thermostat}: {list(settings.keys())}")


# =============================================================================
# SECTION 3: SENSOR-THERMOSTAT COUPLING
# =============================================================================

_coupling_last_sent: dict[str, float] = {}


def _forward_sensor_to_thermostat(name: str, payload: dict) -> None:
    """
    Check if this sensor should forward its temperature to a thermostat.
    Called automatically when any device updates.
    """
    device = None
    for d in config.DEVICES:
        if d["name"] == name:
            device = d
            break

    if not device:
        return

    role = device.get("role")

    for coupling in config.SENSOR_THERMOSTAT_COUPLINGS:
        if coupling["sensor_role"] != role:
            continue

        temperature = payload.get("temperature")
        if temperature is None:
            continue

        temperature = round(temperature, 1)

        cache_key = coupling["thermostat"]
        if cache_key in _coupling_last_sent:
            if abs(temperature - _coupling_last_sent[cache_key]) < 0.1:
                return

        _coupling_last_sent[cache_key] = temperature
        temperature_centidegrees = int(temperature * 100)

        mqtt.send_command(coupling["thermostat"], {
            coupling["field"]: temperature_centidegrees
        })
        logger.info(f"🔗 Forwarded {temperature}°C ({temperature_centidegrees}) → {coupling['thermostat']}")


# Radiator instance — created once at startup from config
_radiator = Radiator(
    rad_type=config.RADIATOR_TYPE,
    height_mm=config.RADIATOR_HEIGHT_MM,
    length_m=config.RADIATOR_LENGTH_M,
    n=config.RADIATOR_N,
)


def _update_radiator_output(name: str, payload: dict) -> None:
    """
    Recalculate radiator heat output whenever any sensor updates.
    Writes result back to state via update_derived().
    """
    t_room   = state.get_by_role("room_temp").get("temperature")
    t_supply = state.get_by_role("supply_temp").get("temperature")
    t_return = state.get_by_role("return_temp").get("temperature")

    if None in [t_room, t_supply, t_return]:
        return

    watts = _radiator.output(t_supply=t_supply, t_return=t_return, t_room=t_room)
    state.update_derived("radiator_1_output", {"watts": watts})


# =============================================================================
# SECTION 4: BACKGROUND TASKS
# =============================================================================

class WeatherScheduler:
    """Periodically fetches weather data and updates the state store."""

    def __init__(self, interval_seconds: int):
        self.interval = interval_seconds
        self.timer = None
        self.running = False

    def start(self):
        self.running = True
        self._run_update()
        logger.info(f"✅ Weather scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()
        logger.info("🛑 Weather scheduler stopped")

    def _run_update(self):
        if not self.running:
            return
        try:
            data = weather_module.fetch_current_weather()
            if data:
                state.update_weather(data)
        except Exception as e:
            logger.error(f"❌ Weather update failed: {e}")
        if self.running:
            self.timer = threading.Timer(self.interval, self._run_update)
            self.timer.daemon = True
            self.timer.start()


class PriceScheduler:
    """Periodically fetches electricity prices and updates the state store."""

    def __init__(self, interval_seconds: int = 900):
        self.interval = interval_seconds
        self.timer = None
        self.running = False

    def start(self):
        self.running = True
        self._run_update()
        logger.info(f"✅ Price scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()
        logger.info("🛑 Price scheduler stopped")

    def _run_update(self):
        if not self.running:
            return
        try:
            data = prices_module.fetch_current_price()
            if data:
                state.update_price(data)
        except Exception as e:
            logger.error(f"❌ Price update failed: {e}")
        if self.running:
            self.timer = threading.Timer(self.interval, self._run_update)
            self.timer.daemon = True
            self.timer.start()


class MpcScheduler:
    """
    Periodically runs MPC optimization and sends setpoint to thermostat.
    Aligned to clock boundaries (:00, :15, :30, :45 for 15-min intervals).
    """

    def __init__(self, interval_seconds: int, enabled: bool = True):
        self.interval = interval_seconds
        self.timer = None
        self.running = False
        self.enabled = enabled

    def _seconds_until_next_slot(self) -> int:
        from datetime import datetime
        now = datetime.now()
        interval_minutes = self.interval // 60
        current_minute = now.minute
        next_aligned_minute = ((current_minute // interval_minutes) + 1) * interval_minutes
        if next_aligned_minute >= 60:
            minutes_to_wait = (60 - current_minute) + (next_aligned_minute - 60)
        else:
            minutes_to_wait = next_aligned_minute - current_minute
        seconds_to_wait = minutes_to_wait * 60 - now.second
        return max(5, seconds_to_wait + 5)

    def start(self):
        if not self.enabled:
            logger.info("⏸️ MPC scheduler disabled")
            return
        self.running = True
        delay = self._seconds_until_next_slot()
        interval_minutes = self.interval // 60
        from datetime import datetime, timedelta
        next_run = datetime.now() + timedelta(seconds=delay)
        self.timer = threading.Timer(delay, self._run_update)
        self.timer.daemon = True
        self.timer.start()
        logger.info(f"✅ MPC scheduler started (every {interval_minutes}min at :00/:15/:30/:45)")
        logger.info(f"   First run at {next_run.strftime('%H:%M:%S')} (in {delay}s)")

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()
        logger.info("🛑 MPC scheduler stopped")

    def _run_update(self):
        if not self.running:
            return
        try:
            # Compute physical max heat from supply curve + outdoor temp.
            # Uses heating curve (config.SUPPLY_TEMP_CURVE) so max_heat reflects
            # what the heat pump can deliver, not the momentary pipe temperature.
            weather  = state.get_weather()
            t_outdoor = weather.get("temp", 0.0) if weather else 0.0
            t_supply  = supply_temp_from_outdoor(t_outdoor)
            t_room    = state.get_by_role("room_temp").get("temperature") or 20.0
            max_heat  = _radiator.max_output(t_supply=t_supply, t_room=t_room)
            logger.info(f"🌡️ t_outdoor={t_outdoor:.1f}°C → t_supply={t_supply:.1f}°C → max_heat={max_heat:.0f}W")

            setpoint = mpc_module.run_mpc_step(max_heat=max_heat)
            if setpoint is not None:
                thermostat_name = config.get_device_name_by_role("thermostat")
                if thermostat_name:
                    mqtt.send_command(thermostat_name, {
                        "occupied_heating_setpoint": setpoint
                    })
                    logger.info(f"🎯 MPC setpoint sent: {setpoint:.1f}°C → {thermostat_name}")
        except Exception as e:
            logger.error(f"❌ MPC update failed: {e}")
        if self.running:
            delay = self._seconds_until_next_slot()
            self.timer = threading.Timer(delay, self._run_update)
            self.timer.daemon = True
            self.timer.start()


# =============================================================================
# SECTION 5: APPLICATION LIFECYCLE
# =============================================================================

class Application:
    """
    Main application class that orchestrates all components.

    Startup order:
        1. InfluxDB
        2. MQTT
        3. Thermostat default settings
        4. Device update callbacks
        5. Weather scheduler
        6. Price scheduler
        7. MPC scheduler
        8. Telegram bot
    """

    def __init__(self):
        self.weather_scheduler = WeatherScheduler(config.WEATHER_UPDATE_INTERVAL)
        self.price_scheduler   = PriceScheduler(config.WEATHER_UPDATE_INTERVAL)
        mpc_interval = config.MPC_CONFIG.get("dt_minutes", 15) * 60
        mpc_enabled  = config.MPC_CONFIG.get("enabled", False)
        self.mpc_scheduler = MpcScheduler(mpc_interval, enabled=mpc_enabled)
        self.assistant     = AIAssistant(state)
        self.telegram_app  = None

    def start(self):
        logger.info("🚀 Starting TroubleHouseBot...")

        influx.start()
        mqtt.start()
        _apply_thermostat_settings()

        state.on_device_update(_forward_sensor_to_thermostat)
        state.on_device_update(_update_radiator_output)
        logger.info("✅ Device update callbacks registered")

        self.weather_scheduler.start()
        self.price_scheduler.start()
        self.mpc_scheduler.start()

        self.telegram_app = create_bot(assistant=self.assistant)

        logger.info("✅ All services started")
        logger.info("📱 Telegram bot is running. Press Ctrl+C to stop.")

        self.telegram_app.run_polling()

    def stop(self):
        logger.info("🛑 Shutting down...")
        self.mpc_scheduler.stop()
        self.weather_scheduler.stop()
        self.price_scheduler.stop()
        mqtt.stop()
        influx.stop()
        logger.info("👋 Goodbye!")


# =============================================================================
# SECTION 6: ENTRY POINT
# =============================================================================

def main():
    # Seed model_params.json from config defaults if this is a fresh install
    from control import model_store

    model_store.init_if_missing()

    app = Application()

    def signal_handler(sig, frame):
        logger.info("\n⚠️ Interrupt received...")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.start()
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        app.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()