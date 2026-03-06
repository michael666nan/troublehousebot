# TroubleHouseBot 🏠

A smart home heating controller running on a Raspberry Pi. Uses Zigbee sensors, MQTT, MPC optimization, and a Telegram bot interface with an AI assistant.

## System Overview

- **Zigbee sensors** — room temperature, supply/return pipe temperatures
- **Danfoss Ally TRV** — thermostatic radiator valve controlled via MQTT
- **MPC optimizer** — runs every 15 minutes using a grey-box thermal model
- **Nord Pool prices** — electricity price forecasting for cost optimization
- **Open-Meteo weather** — forecasts including solar irradiance
- **Telegram bot** — control and monitoring interface
- **AI assistant** — natural language interface powered by Claude

---

## Fresh Pi Setup

### Prerequisites
- Raspberry Pi (tested on Pi 4, Bookworm 64-bit)
- Sonoff Zigbee 3.0 USB Dongle Plus (EFR32MG21)
- GitHub account with access to this repo
- Telegram bot token (from @BotFather)
- Anthropic API key (from console.anthropic.com)

### Step 1 — Basic Pi configuration (manual)
```bash
sudo raspi-config
```
Set hostname, enable SSH, connect to WiFi. Then SSH in from your computer.

### Step 2 — Clone the repo
```bash
git clone https://github.com/michael666nan/troublehousebot.git
cd troublehousebot
```

### Step 3 — Create your `.env`
```bash
cp .env.example .env
nano .env
```
Fill in your three secrets:
```dotenv
INFLUXDB_TOKEN=troublehousebot-influx-token
BOT_TOKEN=your_telegram_bot_token
CLAUDE_API_KEY=your_anthropic_api_key
```

### Step 4 — Run setup
```bash
chmod +x setup.sh
./setup.sh
```
This installs and configures everything:
- Mosquitto MQTT broker
- Zigbee2MQTT (via Docker)
- InfluxDB 2
- Python virtual environment with all dependencies
- Systemd services for auto-start on boot

### Step 5 — Reboot
```bash
sudo reboot
```

After reboot all services start automatically. Pair your Zigbee devices at `http://<pi-ip>:8080`.

---

## Daily Development Workflow

### Making changes on your laptop
```bash
# Edit files, then:
git add .
git commit -m "Description of change"
git push
```

### Deploying to the Pi
```bash
# On the Pi:
cd ~/troublehousebot
git pull
sudo systemctl restart troublehousebot
```

---

## Useful Commands

```bash
# View live logs
journalctl -u troublehousebot -f

# Restart the bot
sudo systemctl restart troublehousebot

# Check service status
sudo systemctl status troublehousebot

# Zigbee2MQTT web UI
http://<pi-ip>:8080

# InfluxDB web UI
http://<pi-ip>:8086
```

---

## Project Structure

```
troublehousebot/
├── main.py                        # Entry point, wires everything together
├── config.py                      # Single source of truth for all settings
├── state.py                       # In-memory state store
├── mqtt.py                        # MQTT client and message routing
├── influx.py                      # InfluxDB logging
├── prices.py                      # Nord Pool electricity prices
├── weather.py                     # Open-Meteo weather forecasts
├── radiator.py                    # Radiator heat output calculations
├── schedules.py                   # Temperature schedule management
├── mpc.py                         # Model Predictive Control optimizer
├── telegram_bot.py                # Telegram bot commands and handlers
├── connectivity.py                # Network connectivity checks
│
├── ai_assistant/                  # AI chat assistant
│   ├── assistant.py               # Main orchestrator
│   ├── conversation.py            # Session management
│   ├── context_builder.py         # System prompt builder
│   ├── llm_client.py              # Claude API wrapper
│   ├── memory.py                  # Persistent conversation history
│   └── tools/
│       ├── definitions.py         # Tool schemas for Claude
│       └── handlers.py            # Tool execution functions
│
├── plots/                         # Interactive Plotly charts
│   └── history.py                 # Historical data charts (InfluxDB)
│
├── .env.example                   # Secrets template (copy to .env)
├── requirements.txt               # Python dependencies
└── setup.sh                       # Full Pi setup script
```

---

## Configuration

All non-secret settings are in `config.py` — devices, radiator parameters, MPC model, tariffs, etc. This is the only file you need to edit when deploying to a different house.

Secrets (API tokens) go in `.env` which is never committed to git. See `.env.example` for the template.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Current temperatures, price, and system status |
| `/weather` | Current weather conditions |
| `/price` | Current electricity price |
| `/schedule` | View heating schedule |
| `/plot <type> [range]` | Historical chart (temperatures/prices/weather) |
| `/mpc` | MPC optimizer status |
| `/set <temp>` | Set temperature setpoint |
| `/chat` | Start AI assistant session |
| `/exit` | Exit AI assistant session |
| `/help` | Show all commands |