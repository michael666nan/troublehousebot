# =============================================================================
# CONFIG - The Single Source of Truth
# =============================================================================
# This file defines ALL devices, their roles, and system constants.
# When deploying to a new house, THIS is the only file you need to edit.
#
# Secrets (tokens, passwords) live in .env — never commit that file.
# Everything else lives here and is safe to commit.
# =============================================================================

import os
from dotenv import load_dotenv
import numpy as np

load_dotenv()


# =============================================================================
# SECTION 1: SECRETS (from .env)
# =============================================================================
# These three values must be set in your .env file.
# See .env.example for the template.

INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")


# =============================================================================
# SECTION 2: DEPLOYMENT CONSTANTS
# =============================================================================
# Not secrets — safe to commit. Change these if deploying to a different house.

# --- Location ---
LATITUDE  = 56.1629
LONGITUDE = 10.2039

# --- MQTT ---
MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

# --- InfluxDB ---
INFLUXDB_URL    = "http://localhost:8086"
INFLUXDB_ORG    = "home"
INFLUXDB_BUCKET = "housebot"

# --- Telegram ---
# List of Telegram user IDs allowed to use the bot.
# Find your ID by messaging @userinfobot on Telegram.
ALLOWED_USERS = [8425877402]


# =============================================================================
# SECTION 3: DEVICE DEFINITIONS
# =============================================================================
# Define what devices exist and what role each plays.
# The "role" field is what the rest of the code uses to find the right sensor.
#
# Roles used in this system:
#   - "thermostat"  : Main room thermostat (reads temp, accepts setpoint)
#   - "room_temp"   : Sensor measuring room air temperature
#   - "supply_temp" : Sensor on radiator supply pipe
#   - "return_temp" : Sensor on radiator return pipe
#
# The "name" field must match the Zigbee2MQTT friendly name exactly.

DEVICES = [
    {
        "name": "thermostat_1",
        "role": "thermostat",
        "description": "Living room thermostat"
    },
    {
        "name": "sensor_1",
        "role": "room_temp",
        "description": "Room air temperature sensor"
    },
    {
        "name": "sensor_2",
        "role": "supply_temp",
        "description": "Radiator supply pipe sensor"
    },
    {
        "name": "sensor_3",
        "role": "return_temp",
        "description": "Radiator return pipe sensor"
    },
]


# =============================================================================
# SECTION 4: DERIVED VALUES
# =============================================================================
# "Virtual sensors" calculated from real sensor data.

DERIVED_VALUES = [
    {
        "name": "radiator_1_output",
        "description": "Calculated heat output from radiator",
        "requires_roles": ["room_temp", "supply_temp", "return_temp"],
        "output_field": "watts"
    },
]


# =============================================================================
# SECTION 5: RADIATOR CONFIGURATION
# =============================================================================
# Parameters for heat output calculation: Q = UA × (ΔT)^n
#
# Radiator types (panel radiators, EN 442):
#   10 = Single panel, no convector fins
#   11 = Single panel, single convector
#   21 = Double panel, single convector
#   22 = Double panel, double convector (most common)
#   33 = Triple panel, triple convector

RADIATOR_TYPE      = 22     # Panel type
RADIATOR_HEIGHT_MM = 600    # Height in mm (300, 400, 500, 600, 900)
RADIATOR_LENGTH_M  = 1.2    # Length in meters
RADIATOR_N         = 1.3    # Radiator exponent (typically 1.2–1.4)


# =============================================================================
# SECTION 6: SENSOR-THERMOSTAT COUPLING
# =============================================================================
# Forward external temperature readings to thermostats.
# Danfoss Ally uses centidegrees (21.5°C = 2150). Value -8000 = disabled.

SENSOR_THERMOSTAT_COUPLINGS = [
    {
        "sensor_role": "room_temp",
        "thermostat":  "thermostat_1",
        "field":       "external_measured_room_sensor",
    },
]


# =============================================================================
# SECTION 7: THERMOSTAT DEFAULT SETTINGS
# =============================================================================
# Sent once on startup to ensure correct thermostat configuration.

THERMOSTAT_DEFAULT_SETTINGS = {
    "thermostat_1": {
        "radiator_covered":       True,   # Room Sensor Mode (use external sensor)
        "window_open_feature":    False,
        "heat_available":         True,
        "algorithm_scale_factor": 5,
    },
}


# =============================================================================
# SECTION 8: SOLAR IRRADIANCE SURFACES
# =============================================================================
# Open-Meteo azimuth convention: 0°=South, -90°=East, 90°=West, ±180°=North

SOLAR_SURFACES = [
    {"name": "south", "tilt": 90, "azimuth":    0},
    {"name": "west",  "tilt": 90, "azimuth":   90},
    {"name": "east",  "tilt": 90, "azimuth":  -90},
    {"name": "north", "tilt": 90, "azimuth":  180},
]


# =============================================================================
# SECTION 9: ELECTRICITY PRICES
# =============================================================================
# Danish price areas: DK1 = Jutland/Funen, DK2 = Zealand/Copenhagen

PRICE_AREA = "DK1"

# Tariffs and taxes in DKK/kWh — update periodically from your electricity bill.
# Grid tariffs vary by company — check yours at: https://tariffer.dk

ELECTRICITY_TARIFFS = {
    # Fixed tariffs (same all hours)
    "system_tariff":       0.054,   # Energinet systemtarif
    "transmission_tariff": 0.058,   # Energinet transmissionstarif
    "electricity_tax":     0.008,   # Elafgift (reduced rate 2024)

    # Time-of-use grid tariffs (NRGi Net, Aarhus area)
    "grid_tariff": {
        "winter": {                  # Oct–Mar
            "low":    0.15,          # 00:00–06:00
            "medium": 0.45,          # 06:00–17:00, 20:00–24:00
            "high":   1.35,          # 17:00–20:00
        },
        "summer": {                  # Apr–Sep
            "low":    0.15,
            "medium": 0.23,
            "high":   0.60,
        },
        "hours": {
            "low":    [0, 1, 2, 3, 4, 5],
            "medium": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 21, 22, 23],
            "high":   [17, 18, 19],
        },
        "winter_months": [1, 2, 3, 10, 11, 12],
    },

    "vat_rate": 0.25,   # 25% Danish VAT
}


# =============================================================================
# SECTION 10: HEAT PUMP SUPPLY TEMPERATURE CURVE
# =============================================================================
# Defines the relationship between outdoor temperature and heat pump supply
# temperature. Used to estimate max radiator output independent of whether
# the radiator is currently flowing.
#
# Format: list of (outdoor_temp, supply_temp) tuples.
# Values between points are linearly interpolated.
# Values outside the range are clamped to the nearest endpoint.
#
# Tune these from observation of your heat pump's behaviour.

SUPPLY_TEMP_CURVE = [
    (-15, 55),   # Very cold day  → high supply temp
    (  0, 45),   # Freezing       → moderate supply temp
    ( 10, 35),   # Mild           → low supply temp
    ( 20, 25),   # Warm           → minimal supply temp
]


# =============================================================================
# SECTION 10: TIMING
# =============================================================================

WEATHER_UPDATE_INTERVAL = 900   # seconds (15 minutes)


# =============================================================================
# SECTION 11: MPC CONFIGURATION
# =============================================================================

MPC_CONFIG = {
    "enabled":        True,
    "dt_minutes":     15,       # Can not be changed!
    "horizon_hours":  48,
    "cop":            4.0,
    "slack_penalty":  1e4,
    "state_file":     "mpc_state.json",
}

# Grey-box thermal model (2R2C)
# States: x = [Ti, Tm]  (indoor air temp, thermal mass temp)
# Inputs: u = [Ta, Phi_s, Phi_h]  (ambient, solar, heating)

MPC_MODEL = {
    "A":  10.0,     # [m²] Floor area
    "ha":  0.5,     # [W/(K·m²)] Conductance ambient ↔ indoor
    "hm":  2.0,     # [W/(K·m²)] Conductance indoor ↔ thermal mass
    "ci": 10.0,     # [Wh/(K·m²)] Specific indoor air heat capacity
    "cm": 100.0,    # [Wh/(K·m²)] Specific thermal mass heat capacity
    "p":   0.5,     # [-] Fraction of heat to air vs thermal mass
    "gA":  0,     # [m²] Effective solar aperture (window area × g-value)

    "K": np.array([
        [0.5],      # Kalman correction to Ti
        [0.1],      # Kalman correction to Tm
    ]),

    "x0": np.array([
        [20.0],     # Ti initial guess [°C]
        [18.0],     # Tm initial guess [°C]
    ]),
}


# =============================================================================
# SECTION 12: AI ASSISTANT
# =============================================================================

AI_MODEL                   = "claude-opus-4-6"
AI_MAX_TOKENS              = 1024
AI_MAX_HISTORY_MESSAGES    = 20
AI_MEMORY_SEED_MESSAGES    = 0
AI_SESSION_TIMEOUT_MINUTES = 60


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_device_names() -> list[str]:
    """Returns list of all device names (for MQTT subscription)."""
    return [d["name"] for d in DEVICES]


def get_device_by_role(role: str) -> dict | None:
    """Find a device by its role."""
    for device in DEVICES:
        if device["role"] == role:
            return device
    return None


def get_device_name_by_role(role: str) -> str | None:
    """Shortcut to get just the name for a role."""
    device = get_device_by_role(role)
    return device["name"] if device else None