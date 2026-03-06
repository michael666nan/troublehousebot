#!/bin/bash
# =============================================================================
# TroubleHouseBot - Raspberry Pi Setup Script
# =============================================================================
#
# PURPOSE:
#   Full infrastructure setup for a fresh Pi:
#     - Mosquitto MQTT broker
#     - Zigbee2MQTT (in Docker)
#     - InfluxDB 2 time-series database
#     - Python virtual environment
#     - Systemd services for auto-start
#
# TARGET:
#   Raspberry Pi OS Lite (Bookworm 64-bit recommended)
#   Tested with: Sonoff Zigbee 3.0 USB Dongle Plus (EFR32MG21)
#
# USAGE:
#   git clone https://github.com/YOUR_USERNAME/troublehousebot.git
#   cd troublehousebot
#   cp .env.example .env && nano .env   # Fill in your secrets
#   chmod +x setup.sh && ./setup.sh
#
# =============================================================================

set -e


# =============================================================================
# SECTION 1: CONSTANTS (not secrets — safe to hardcode)
# =============================================================================

INFLUXDB_ORG="home"
INFLUXDB_BUCKET="housebot"
INFLUXDB_URL="http://localhost:8086"
TIMEZONE="Europe/Copenhagen"
CURRENT_USER=$(whoami)
BOT_DIR="/home/$CURRENT_USER/troublehousebot"
ZIGBEE_CHANNEL=25
ZIGBEE_PAN_ID="0x1a2b"


# =============================================================================
# SECTION 2: COLLECT SECRETS INTERACTIVELY
# =============================================================================
# Secrets are never hardcoded. We prompt once, use them to initialise
# InfluxDB, and write them to .env.

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  🔑 Secrets Setup"
echo "══════════════════════════════════════════════════════════════"
echo "  These will be written to .env and used for InfluxDB setup."
echo "  Nothing is stored in this script."
echo ""

# Check if .env already exists — use existing values if so
if [ -f "$BOT_DIR/.env" ]; then
    echo "  ✅ .env already exists — loading existing secrets."
    source "$BOT_DIR/.env"
    INFLUX_ADMIN_PASS=${INFLUX_ADMIN_PASS:-""}
    INFLUXDB_TOKEN="troublehousebot-influx-token"
    BOT_TOKEN=${BOT_TOKEN:-""}
    CLAUDE_API_KEY=${CLAUDE_API_KEY:-""}
else
    read -p "  InfluxDB admin username [admin]: " INFLUX_ADMIN_USER
    INFLUX_ADMIN_USER=${INFLUX_ADMIN_USER:-"admin"}

    read -s -p "  InfluxDB admin password: " INFLUX_ADMIN_PASS
    echo ""

    INFLUXDB_TOKEN="troublehousebot-influx-token"

    read -s -p "  Telegram bot token: " BOT_TOKEN
    echo ""

    read -s -p "  Anthropic API key: " CLAUDE_API_KEY
    echo ""
fi

echo ""


# =============================================================================
# SECTION 3: PRE-FLIGHT CHECKS
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  🏠 TroubleHouseBot Setup"
echo "══════════════════════════════════════════════════════════════"
echo ""

if [ "$EUID" -eq 0 ]; then
    echo "❌ ERROR: Don't run this script as root!"
    exit 1
fi

# --- Check: .env exists and has required keys ---
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "❌ ERROR: .env file not found!"
    echo ""
    echo "   Create it first:"
    echo "     cp .env.example .env"
    echo "     nano .env"
    exit 1
fi

MISSING=""
for key in BOT_TOKEN CLAUDE_API_KEY; do
    if ! grep -q "^${key}=.\+" "$BOT_DIR/.env"; then
        MISSING="$MISSING $key"
    fi
done

if [ -n "$MISSING" ]; then
    echo "❌ ERROR: Missing required values in .env:$MISSING"
    echo ""
    echo "   Edit your .env file and fill in all required secrets."
    exit 1
fi

echo "✅ .env looks good"

echo "🔍 Detecting Zigbee dongle..."
SERIAL_ID=$(ls /dev/serial/by-id/*Sonoff* 2>/dev/null | head -n1 || true)

if [ -z "$SERIAL_ID" ]; then
    echo "❌ ERROR: Sonoff Zigbee dongle not found!"
    echo "   Check with: ls /dev/serial/by-id/"
    exit 1
fi
echo "✅ Found dongle: $SERIAL_ID"
echo ""


# =============================================================================
# SECTION 4: SYSTEM PACKAGES
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  📦 Step 1/6: Installing System Packages"
echo "══════════════════════════════════════════════════════════════"

sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    python3-pip \
    python3-venv \
    mosquitto \
    mosquitto-clients \
    curl \
    gpg \
    openssl \
    libopenjp2-7

echo "✅ System packages installed"
echo ""


# =============================================================================
# SECTION 5: DOCKER
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  🐳 Step 2/6: Installing Docker"
echo "══════════════════════════════════════════════════════════════"

if command -v docker &> /dev/null; then
    echo "⏭️  Docker already installed, skipping..."
else
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker $CURRENT_USER
    echo "✅ Docker installed"
fi
echo ""


# =============================================================================
# SECTION 6: ZIGBEE2MQTT
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  📡 Step 3/6: Configuring Zigbee2MQTT"
echo "══════════════════════════════════════════════════════════════"

sudo mkdir -p /opt/zigbee2mqtt/data
sudo chown -R $CURRENT_USER:$CURRENT_USER /opt/zigbee2mqtt

cat > /opt/zigbee2mqtt/data/configuration.yaml << EOF
homeassistant: false
permit_join: true
mqtt:
  base_topic: zigbee2mqtt
  server: 'mqtt://localhost:1883'
serial:
  port: /dev/ttyUSB0
  adapter: ember
frontend:
  port: 8080
availability: true
advanced:
  last_seen: ISO_8601
  channel: $ZIGBEE_CHANNEL
  pan_id: $ZIGBEE_PAN_ID
  ext_pan_id: [0xDD, 0xDD, 0xDD, 0xDD, 0xDD, 0xDD, 0xDD, 0xDD]
  network_key: [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31]
EOF

cat > /opt/zigbee2mqtt/docker-compose.yml << EOF
version: '3.8'
services:
  zigbee2mqtt:
    container_name: zigbee2mqtt
    image: koenkk/zigbee2mqtt
    restart: unless-stopped
    network_mode: host
    volumes:
      - /opt/zigbee2mqtt/data:/app/data
      - /run/udev:/run/udev:ro
    devices:
      - ${SERIAL_ID}:/dev/ttyUSB0
    environment:
      - TZ=$TIMEZONE
EOF

echo "✅ Zigbee2MQTT configured"
echo ""


# =============================================================================
# SECTION 7: INFLUXDB
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  📊 Step 4/6: Installing InfluxDB 2"
echo "══════════════════════════════════════════════════════════════"

if command -v influx &> /dev/null; then
    echo "⏭️  InfluxDB already installed, skipping..."
else
    curl -fsSL https://repos.influxdata.com/influxdata-archive.key \
        | sudo gpg --dearmor -o /usr/share/keyrings/influxdb-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/influxdb-archive-keyring.gpg] https://repos.influxdata.com/debian stable main" \
        | sudo tee /etc/apt/sources.list.d/influxdb.list
    sudo apt update && sudo apt install -y influxdb2
    echo "✅ InfluxDB installed"
fi

sudo systemctl enable --now influxdb

echo "⏳ Waiting for InfluxDB to start..."
sleep 5
for i in {1..10}; do
    if curl -s http://localhost:8086/health > /dev/null 2>&1; then break; fi
    sleep 2
done

influx setup \
    --username "$INFLUX_ADMIN_USER" \
    --password "$INFLUX_ADMIN_PASS" \
    --org "$INFLUXDB_ORG" \
    --bucket "$INFLUXDB_BUCKET" \
    --token "$INFLUXDB_TOKEN" \
    --force 2>/dev/null || echo "   (Already initialized)"

echo "✅ InfluxDB ready"
echo ""


# =============================================================================
# SECTION 8: PYTHON ENVIRONMENT
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  🐍 Step 5/6: Setting Up Python Environment"
echo "══════════════════════════════════════════════════════════════"

cd "$BOT_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "✅ Python environment ready"
echo ""


# =============================================================================
# SECTION 9: WRITE .ENV
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  📝 Writing .env"
echo "══════════════════════════════════════════════════════════════"

cat > "$BOT_DIR/.env" << EOF
# Generated by setup.sh on $(date)
# Do not commit this file to git.

INFLUXDB_TOKEN=$INFLUXDB_TOKEN
BOT_TOKEN=$BOT_TOKEN
CLAUDE_API_KEY=$CLAUDE_API_KEY
EOF

chmod 600 "$BOT_DIR/.env"   # Owner read/write only
echo "✅ .env written (chmod 600)"
echo ""


# =============================================================================
# SECTION 10: SYSTEMD SERVICES
# =============================================================================

echo "══════════════════════════════════════════════════════════════"
echo "  ⚙️  Step 6/6: Creating System Services"
echo "══════════════════════════════════════════════════════════════"

sudo tee /etc/systemd/system/zigbee2mqtt.service > /dev/null << EOF
[Unit]
Description=Zigbee2MQTT
After=docker.service mosquitto.service
Requires=docker.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=/opt/zigbee2mqtt
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/troublehousebot.service > /dev/null << EOF
[Unit]
Description=TroubleHouseBot
After=network.target mosquitto.service influxdb.service zigbee2mqtt.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python main.py
Environment=MPLCONFIGDIR=$BOT_DIR/.matplotlib
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$BOT_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable zigbee2mqtt troublehousebot

echo "✅ Services created and enabled"
echo ""


# =============================================================================
# SUMMARY
# =============================================================================

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  ✅ SETUP COMPLETE"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  📁 Bot directory:   $BOT_DIR"
echo "  📡 Zigbee2MQTT UI:  http://<pi-ip>:8080"
echo "  📊 InfluxDB UI:     http://<pi-ip>:8086"
echo ""
echo "  Next step: sudo reboot"
echo ""
echo "══════════════════════════════════════════════════════════════"