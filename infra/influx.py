# =============================================================================
# INFLUX - Time-Series Database Logging
# =============================================================================
# This module handles ALL InfluxDB communication:
#   - Connection setup
#   - Logging sensor data
#   - Logging derived values
#   - Logging weather data
#
# It does NOT:
#   - Store state in memory (that's state.py)
#   - Make decisions about what to log (it logs what it's told)
# =============================================================================

import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

import config
from state import state

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: INFLUXDB CLIENT CLASS
# =============================================================================

class InfluxLogger:
    """
    Handles logging to InfluxDB.
    
    Automatically registers itself as a listener on the state store,
    so it logs every state change without other modules needing to know.
    """

    def __init__(self):
        self.enabled = False
        self.client = None
        self.write_api = None

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self):
        """
        Initialize InfluxDB connection and register state callbacks.
        
        If InfluxDB is not configured, the logger runs in disabled mode
        (all writes silently succeed without doing anything).
        """
        # Check if InfluxDB is configured
        if not all([config.INFLUXDB_URL, config.INFLUXDB_TOKEN, config.INFLUXDB_ORG]):
            logger.warning("⚠️ InfluxDB not configured - logging disabled")
            return

        try:
            self.client = InfluxDBClient(
                url=config.INFLUXDB_URL,
                token=config.INFLUXDB_TOKEN,
                org=config.INFLUXDB_ORG
            )
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.enabled = True
            logger.info("✅ InfluxDB connected")
            
            # Register as listener for all state changes
            state.on_device_update(self._log_measurement)
            state.on_derived_update(self._log_measurement)
            state.on_weather_update(self._log_measurement)
            state.on_price_update(self._log_measurement)
            
        except Exception as e:
            logger.error(f"❌ InfluxDB connection failed: {e}")
            self.enabled = False

    def stop(self):
        """Close InfluxDB connection gracefully."""
        if self.client:
            self.client.close()
            logger.info("🛑 InfluxDB disconnected")

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------

    def _log_measurement(self, name: str, data: dict):
        """
        Log a measurement to InfluxDB.

        For zone devices: writes under the role name (e.g. "room_temp") with
        a zone tag (e.g. zone=zone_1). This keeps InfluxDB measurement names
        clean and role-based, with zone separation via tags.

        For non-zone measurements (weather, prices, derived): writes under
        the original name unchanged.
        """
        if not self.enabled:
            return

        zone_id = config.get_zone_for_device(name)
        role    = config.get_role_for_device(name)

        # Derived measurements (e.g. zone_1_radiator_output) — look up zone separately
        if not zone_id:
            zone_id = config.get_zone_for_derived(name)

        # Determine measurement name for InfluxDB:
        # - Zone devices: use role (e.g. "room_temp") — clean, zone separated by tag
        # - Zone derived: strip zone prefix (e.g. "zone_1_radiator_output" → "radiator_output")
        # - Everything else (weather, prices): use original name unchanged
        if zone_id and role:
            measurement = role
        elif zone_id and name.startswith(f"{zone_id}_"):
            measurement = name[len(f"{zone_id}_"):]
        else:
            measurement = name

        point = Point(measurement)
        if zone_id:
            point.tag("zone", zone_id)

        has_data = False
        for key, value in data.items():
            if isinstance(value, (int, float)):
                point.field(key, float(value))
                has_data = True

        if not has_data:
            return

        try:
            self.write_api.write(bucket=config.INFLUXDB_BUCKET, record=point)
            logger.debug(f"Logged to InfluxDB: {measurement} (zone={zone_id})")
        except Exception as e:
            logger.error(f"InfluxDB write failed: {e}")


# =============================================================================
# SECTION 2: GLOBAL INSTANCE
# =============================================================================
# Single instance used by main.py to start/stop.

influx = InfluxLogger()