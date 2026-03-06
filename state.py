# =============================================================================
# STATE - Central State Store
# =============================================================================
# This is the "memory" of the system. All sensor readings, weather data,
# prices, and calculated values live here.
#
# Design principles:
#   - Pure data store: no physics, no business logic, no external dependencies
#   - Observer pattern: modules register callbacks instead of being imported
#   - Persistence: state is saved to JSON on every update and loaded on startup
#
# Data flow example:
#   1. Zigbee sensor sends temperature → MQTT broker
#   2. mqtt.py receives message → calls state.update_device("sensor_1", {...})
#   3. state.py stores data, fires callbacks, saves to disk
#   4. influx.py callback logs to InfluxDB
#   5. main.py radiator callback recalculates derived values
#   6. bot.py calls state.get_device("sensor_1") → returns stored data
# =============================================================================

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import config

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "state_persist.json")


# =============================================================================
# SECTION 1: PERSISTENCE
# =============================================================================

def _save_to_disk(data: dict) -> None:
    """Write state to disk atomically via a temp file."""
    try:
        tmp_path = _STATE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, _STATE_FILE)
    except Exception as e:
        logger.warning(f"⚠️ Could not save state to disk: {e}")


def _load_from_disk() -> dict:
    """Load persisted state from disk. Returns empty dict on first run or error."""
    if not os.path.exists(_STATE_FILE):
        logger.info("ℹ️ No persisted state found, starting fresh")
        return {}
    try:
        with open(_STATE_FILE, "r") as f:
            data = json.load(f)
        logger.info(f"✅ Loaded persisted state from {_STATE_FILE}")
        return data
    except Exception as e:
        logger.warning(f"⚠️ Could not load persisted state: {e} — starting fresh")
        return {}


# =============================================================================
# SECTION 2: STATE STORE
# =============================================================================

@dataclass
class StateStore:
    """
    Central storage for all runtime data.

    Stores device readings, derived values, weather, and prices.
    Notifies registered callbacks on every update.
    Persists to JSON on every update and loads on startup.
    """

    devices: dict[str, dict] = field(default_factory=dict)
    derived: dict[str, dict] = field(default_factory=dict)
    weather: dict[str, Any] = field(default_factory=dict)
    price:   dict[str, Any] = field(default_factory=dict)

    _on_device_update:  list[Callable] = field(default_factory=list)
    _on_derived_update: list[Callable] = field(default_factory=list)
    _on_weather_update: list[Callable] = field(default_factory=list)
    _on_price_update:   list[Callable] = field(default_factory=list)

    def __post_init__(self):
        # Pre-populate devices so get_device() never crashes on unknown names
        for name in config.get_device_names():
            self.devices[name] = {}

        # Restore last known state from disk
        self._load()

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load(self) -> None:
        data = _load_from_disk()
        if not data:
            return
        self.devices = data.get("devices", self.devices)
        self.derived = data.get("derived", self.derived)
        self.weather = data.get("weather", self.weather)
        self.price   = data.get("price",   self.price)

    def _save(self) -> None:
        _save_to_disk({
            "devices": self.devices,
            "derived": self.derived,
            "weather": self.weather,
            "price":   self.price,
        })

    # =========================================================================
    # Update methods (called by mqtt.py, weather.py, prices.py)
    # =========================================================================

    def update_device(self, name: str, payload: dict):
        self.devices[name] = payload
        for cb in self._on_device_update:
            cb(name, payload)
        self._save()

    def update_derived(self, name: str, payload: dict):
        self.derived[name] = payload
        for cb in self._on_derived_update:
            cb(name, payload)
        self._save()

    def update_weather(self, data: dict):
        self.weather = data
        for cb in self._on_weather_update:
            cb("outside_weather", data)
        self._save()

    def update_price(self, data: dict):
        self.price = data
        for cb in self._on_price_update:
            cb("electricity_price", data)
        self._save()

    # =========================================================================
    # Read methods (called by bot.py, ai_assistant, etc.)
    # =========================================================================

    def get_device(self, name: str) -> dict:
        return self.devices.get(name, {})

    def get_by_role(self, role: str) -> dict:
        name = config.get_device_name_by_role(role)
        return self.get_device(name) if name else {}

    def get_derived(self, name: str) -> dict:
        return self.derived.get(name, {})

    def get_weather(self) -> dict:
        return self.weather

    def get_price(self) -> dict:
        return self.price

    # =========================================================================
    # Callback registration
    # =========================================================================

    def on_device_update(self, callback: Callable):
        self._on_device_update.append(callback)

    def on_derived_update(self, callback: Callable):
        self._on_derived_update.append(callback)

    def on_weather_update(self, callback: Callable):
        self._on_weather_update.append(callback)

    def on_price_update(self, callback: Callable):
        self._on_price_update.append(callback)


# =============================================================================
# SECTION 3: GLOBAL INSTANCE
# =============================================================================

state = StateStore()