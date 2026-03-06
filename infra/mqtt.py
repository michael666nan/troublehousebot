# =============================================================================
# MQTT - Message Broker Communication
# =============================================================================
# This module handles ALL MQTT communication:
#   - Subscribing to Zigbee2MQTT device topics
#   - Routing incoming messages to the state store
#   - Publishing commands back to devices
#
# It does NOT:
#   - Store state (that's state.py)
#   - Log to databases (that's influx.py)
#   - Calculate derived values (that's state.py)
# =============================================================================

import json
import logging
import paho.mqtt.client as mqtt_lib

import config
from state import state

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: MQTT CLIENT CLASS
# =============================================================================

class MQTTClient:
    """
    Manages the MQTT connection to Zigbee2MQTT.
    
    Responsibilities:
        - Connect to broker
        - Subscribe to device topics
        - Route messages to state store
        - Provide send_command() for outgoing messages
    """

    def __init__(self):
        self.client = mqtt_lib.Client()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self):
        """Connect to MQTT broker and start the background loop."""
        try:
            self.client.connect(config.MQTT_BROKER, config.MQTT_PORT)
            self.client.loop_start()
            logger.info(f"✅ MQTT connecting to {config.MQTT_BROKER}:{config.MQTT_PORT}")
        except Exception as e:
            logger.error(f"❌ MQTT connection failed: {e}")
            raise

    def stop(self):
        """Gracefully disconnect from MQTT."""
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("🛑 MQTT disconnected")

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        """Called when connection to broker is established."""
        if rc == 0:
            self.connected = True
            # Subscribe to all Zigbee2MQTT device topics
            client.subscribe("zigbee2mqtt/+")
            logger.info("✅ MQTT connected and subscribed")
        else:
            logger.error(f"❌ MQTT connection failed with code {rc}")

    def _on_message(self, client, userdata, msg):
        """
        Called for each incoming MQTT message.
        
        Filters for watched devices and routes to state store.
        """
        try:
            # Extract device name from topic: "zigbee2mqtt/sensor_1" -> "sensor_1"
            device_name = msg.topic.split('/')[-1]
            
            # Ignore devices we don't care about
            if device_name not in config.get_all_device_names():
                return
            
            # Parse JSON payload
            payload = json.loads(msg.payload.decode())
            
            # Route to state store (which will trigger derived calculations)
            state.update_device(device_name, payload)
            
            logger.debug(f"📥 {device_name}: {payload}")
            
        except json.JSONDecodeError:
            logger.warning(f"⚠️ Invalid JSON from {msg.topic}")
        except Exception as e:
            logger.error(f"❌ Error processing message: {e}")

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    def send_command(self, device_name: str, command: dict):
        """
        Send a command to a Zigbee device.
        
        Args:
            device_name: The Zigbee2MQTT friendly name
            command: Dict of attributes to set, e.g. {"occupied_heating_setpoint": 21.5}
        """
        topic = f"zigbee2mqtt/{device_name}/set"
        payload = json.dumps(command)
        
        self.client.publish(topic, payload)
        logger.info(f"📤 Sent to {device_name}: {command}")


# =============================================================================
# SECTION 2: GLOBAL INSTANCE
# =============================================================================
# Single instance used by other modules.

mqtt = MQTTClient()