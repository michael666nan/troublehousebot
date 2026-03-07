# =============================================================================
# CONNECTIVITY - MQTT & InfluxDB IO
# =============================================================================
import os, json, logging
import paho.mqtt.client as mqtt_lib
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from dotenv import load_dotenv

import physics

load_dotenv()

# We only care about these 4 devices
WATCHED_DEVICES = ["thermostat_1", "sensor_1", "sensor_2", "sensor_3"]

class Connectivity:
    """Handles real-time MQTT data and historical InfluxDB logging."""
    
    def __init__(self):
        self.mqtt = mqtt_lib.Client()
        self.states = {name: {} for name in WATCHED_DEVICES} # Pre-fill names
        
        # Setup InfluxDB
        self.influx_enabled = False
        try:
            self.influx = InfluxDBClient(
                url=os.getenv("INFLUXDB_URL"), 
                token=os.getenv("INFLUXDB_TOKEN"), 
                org=os.getenv("INFLUXDB_ORG")
            )
            self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)
            self.bucket = os.getenv("INFLUXDB_BUCKET")
            self.influx_enabled = True
            print("✅ InfluxDB Ready")
        except Exception as e:
            print(f"⚠️ InfluxDB Error: {e}")

    def start(self):
        """Connect to MQTT and start the background loop."""
        self.mqtt.on_message = self._on_message
        self.mqtt.connect(os.getenv("MQTT_BROKER", "localhost"), 1883)
        self.mqtt.subscribe("zigbee2mqtt/+") # Listen to all top-level device topics
        self.mqtt.loop_start()
        print("✅ MQTT Connected")

    def _on_message(self, client, userdata, msg):
        try:
            name = msg.topic.split('/')[-1]
            if name in WATCHED_DEVICES:
                payload = json.loads(msg.payload.decode())
                self.states[name] = payload
                
                # 1. Log the raw sensor update
                self._log_to_influx(name, payload)

                # 2. Recalculate the Radiator Proxy using latest known temperatures
                q = physics.calculate_radiator_output(
                    t_room   = self.states["sensor_1"].get("temperature"),
                    t_supply = self.states["sensor_2"].get("temperature"),
                    t_return = self.states["sensor_3"].get("temperature")
                )

                # 3. If the math worked, save to memory and InfluxDB
                if q is not None:
                    self.states["radiator_1_output"] = {"watts": q}
                    self._log_to_influx("radiator_1_output", {"watts": q})
                    
        except Exception as e:
            print(f"Error: {e}")

    def _log_to_influx(self, name, data):
        """Logs every numeric value in the JSON payload to InfluxDB."""
        if not self.influx_enabled: return
        
        point = Point(name)
        valid_data = False
        for key, value in data.items():
            if isinstance(value, (int, float)):
                point.field(key, float(value))
                valid_data = True
        
        if valid_data:
            try:
                self.write_api.write(bucket=self.bucket, record=point)
            except: pass

    def send_cmd(self, name, command):
        """Utility to send a command back to a device (e.g. set temp)."""
        self.mqtt.publish(f"zigbee2mqtt/{name}/set", json.dumps(command))

# Global instance for the rest of the app
conn = Connectivity()