# =============================================================================
# CONFIG - The Single Source of Truth
# =============================================================================
# This file defines ALL zones, devices, and system constants.
# When deploying to a new house, THIS is the only file you need to edit.
#
# KEY CONCEPTS:
#   Zone internal ID  (e.g. "zone_1")  — used in code, MQTT, InfluxDB tags.
#                                         Set once at install, never change.
#   Zone display name (e.g. "Living Room") — shown in Telegram and AI chat.
#                                             Change freely at any time.
#
# To add a second zone later, just add another entry to ZONES below and
# restart the bot. No other code changes needed.
#
# Secrets (tokens, passwords) live in .env — never commit that file.
# =============================================================================

import os
from dotenv import load_dotenv

load_dotenv()


# =============================================================================
# SECTION 1: SECRETS (from .env)
# =============================================================================

INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")


# =============================================================================
# SECTION 2: DEPLOYMENT CONSTANTS
# =============================================================================

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
# SECTION 3: ZONES
# =============================================================================
# Each zone has:
#   display_name        -- human-readable name shown in Telegram and AI chat
#   devices             -- maps role to Zigbee2MQTT device name (must be unique!)
#   thermostat_settings -- sent to thermostat on startup
#   radiator            -- EN 442 panel radiator parameters
#   mpc                 -- MPC scheduler settings
#   mpc_model           -- 2R2C grey-box thermal model initial parameters
#
# Device roles:
#   "thermostat"  : Danfoss Ally TRV (reads temp, accepts setpoint)
#   "room_temp"   : External room temperature sensor
#   "supply_temp" : Sensor on radiator supply pipe   (optional)
#   "return_temp" : Sensor on radiator return pipe   (optional)
#
# Partial zones (e.g. bedroom with only thermostat + room_temp) work fine.
# Radiator output calculation is skipped gracefully if pipe sensors are absent.
#
# Radiator types (panel radiators, EN 442):
#   10 = Single panel, no convector fins
#   11 = Single panel, single convector
#   21 = Double panel, single convector
#   22 = Double panel, double convector  (most common)
#   33 = Triple panel, triple convector

ZONES = {
    "zone_1": {
        # ---- Identity --------------------------------------------------------
        "display_name": "Office",

        # ---- Devices ---------------------------------------------------------
        # Values must match Zigbee2MQTT friendly names exactly.
        "devices": {
            "thermostat":  "zone_1_thermostat",
            "room_temp":   "zone_1_room_temp",
            "supply_temp": "zone_1_supply_temp",
            "return_temp": "zone_1_return_temp",
        },

        # ---- Thermostat settings (sent on startup) ---------------------------
        "thermostat_settings": {
            "radiator_covered":       True,
            "window_open_feature":    False,
            "heat_available":         True,
            "algorithm_scale_factor": 5,
        },

        # ---- Radiator physics ------------------------------------------------
        "radiator": {
            "name":      "radiator_output",   # InfluxDB measurement name (zone tag added automatically)
            "rad_type":  22,
            "height_mm": 600,
            "length_m":  1.2,
            "n":         1.3,
        },

        # ---- MPC settings ----------------------------------------------------
        "mpc": {
            "enabled":       True,
            "dt_minutes":    15,
            "horizon_hours": 48,
            "cop":           4.0,
            "slack_penalty": 1e4,
        },

        # ---- Grey-box thermal model (2R2C) initial parameters ----------------
        "mpc_model": {
            "A":   10.0,
            "ha":   0.5,
            "hm":   2.0,
            "ci":  10.0,
            "cm": 100.0,
            "p":    0.5,
            "gA":   0.0,
            "K":  [0.5, 0.1],
            "x0": [20.0, 18.0],
        },
    },

    "zone_2": {
        # ---- Identity --------------------------------------------------------
        "display_name": "Bedroom",           # ← change to actual room name

        # ---- Devices ---------------------------------------------------------
        # Set these to match your Zigbee2MQTT friendly names exactly.
        # Remove roles that don't exist in this zone (e.g. supply/return temp).
        "devices": {
            "thermostat":  "zone_2_thermostat",
            "room_temp":   "zone_2_room_temp",
            # "supply_temp": "zone_2_supply_temp",   # uncomment if available
            # "return_temp": "zone_2_return_temp",   # uncomment if available
        },

        # ---- Thermostat settings (sent on startup) ---------------------------
        "thermostat_settings": {
            "radiator_covered":       True,
            "window_open_feature":    False,
            "heat_available":         True,
            "algorithm_scale_factor": 5,
        },

        # ---- Radiator physics ------------------------------------------------
        # Adjust rad_type, height_mm, length_m to match the actual radiator.
        # See EN 442 panel types in the comment block above ZONES.
        "radiator": {
            "name":      "radiator_output",
            "rad_type":  22,
            "height_mm": 600,
            "length_m":  1.0,
            "n":         1.3,
        },

        # ---- MPC settings ----------------------------------------------------
        # Start with enabled: False until sysid has been run for this zone.
        "mpc": {
            "enabled":       False,
            "dt_minutes":    15,
            "horizon_hours": 48,
            "cop":           4.0,
            "slack_penalty": 1e4,
        },

        # ---- Grey-box thermal model (2R2C) initial parameters ----------------
        # These are reasonable defaults — run /sysid to refine them.
        "mpc_model": {
            "A":   10.0,
            "ha":   0.5,
            "hm":   2.0,
            "ci":  10.0,
            "cm": 100.0,
            "p":    0.5,
            "gA":   0.0,
            "K":  [0.5, 0.1],
            "x0": [20.0, 18.0],
        },
    },

    # ---- Add more zones here -------------------------------------------------
    # "zone_3": {
    #     "display_name": "Office",
    #     "devices": {
    #         "thermostat": "zone_3_thermostat",
    #         "room_temp":  "zone_3_room_temp",
    #     },
    #     ...
    # },
}


# =============================================================================
# SECTION 4: SOLAR IRRADIANCE SURFACES
# =============================================================================
# Open-Meteo azimuth convention: 0=South, -90=East, 90=West, 180=North

SOLAR_SURFACES = [
    {"name": "south", "tilt": 90, "azimuth":    0},
    {"name": "west",  "tilt": 90, "azimuth":   90},
    {"name": "east",  "tilt": 90, "azimuth":  -90},
    {"name": "north", "tilt": 90, "azimuth":  180},
]


# =============================================================================
# SECTION 5: ELECTRICITY PRICES
# =============================================================================
# Danish price areas: DK1 = Jutland/Funen, DK2 = Zealand/Copenhagen

PRICE_AREA = "DK1"

ELECTRICITY_TARIFFS = {
    "system_tariff":       0.054,
    "transmission_tariff": 0.058,
    "electricity_tax":     0.008,

    "grid_tariff": {
        "winter": {
            "low":    0.15,
            "medium": 0.45,
            "high":   1.35,
        },
        "summer": {
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

    "vat_rate": 0.25,
}


# =============================================================================
# SECTION 6: HEAT PUMP SUPPLY TEMPERATURE CURVE
# =============================================================================
# Relationship between outdoor temperature and heat pump supply temperature.
# Format: list of (outdoor_temp, supply_temp) tuples -- linearly interpolated.

SUPPLY_TEMP_CURVE = [
    (-15, 55),
    (  0, 45),
    ( 10, 35),
    ( 20, 25),
]


# =============================================================================
# SECTION 7: TIMING
# =============================================================================

WEATHER_UPDATE_INTERVAL = 900   # seconds (15 minutes)


# =============================================================================
# SECTION 8: AI ASSISTANT
# =============================================================================

AI_MODEL                   = "claude-sonnet-4-6"
AI_MAX_TOKENS              = 1024
AI_MAX_HISTORY_MESSAGES    = 20
AI_MEMORY_SEED_MESSAGES    = 0
AI_SESSION_TIMEOUT_MINUTES = 60


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_zone_ids() -> list[str]:
    """Return all configured zone IDs."""
    return list(ZONES.keys())


def get_first_zone_id() -> str:
    """Return the first zone ID (default for single-zone commands)."""
    return next(iter(ZONES))


def get_all_device_names() -> list[str]:
    """Return all device names across all zones (for MQTT subscription)."""
    names = []
    for zone_cfg in ZONES.values():
        names.extend(zone_cfg["devices"].values())
    return names


def get_zone_for_device(device_name: str) -> str | None:
    """Return the zone_id that owns a given device name, or None."""
    for zone_id, zone_cfg in ZONES.items():
        if device_name in zone_cfg["devices"].values():
            return zone_id
    return None


def get_role_for_device(device_name: str) -> str | None:
    """Return the role (e.g. 'room_temp') for a given device name, or None."""
    for zone_cfg in ZONES.values():
        for role, name in zone_cfg["devices"].items():
            if name == device_name:
                return role
    return None


def get_zone_for_derived(derived_name: str) -> str | None:
    """
    Return the zone_id for a derived state key (e.g. "zone_1_radiator_output").
    The state key is always zone-scoped: {zone_id}_radiator_output.
    """
    for zone_id in ZONES:
        if derived_name == f"{zone_id}_radiator_output":
            return zone_id
    return None


def get_zone_display_name(zone_id: str) -> str:
    """Return the display name for a zone."""
    return ZONES[zone_id]["display_name"]