# =============================================================================
# TELEGRAM BOT - User Interface
# =============================================================================
# Handles all Telegram interaction:
#   - Command handlers (/status, /set, /help, etc.)
#   - Plain text messages (forwarded to AI assistant when in chat mode)
#   - Message formatting and user authorization
#
# It does NOT:
#   - Start the application (that's main.py)
#   - Store state (reads from state.py)
#   - Schedule tasks (that's main.py)
#   - Manage AI sessions (that's ai_assistant/assistant.py)
# =============================================================================

import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import config
from state import state
from infra.mqtt import mqtt
from forecasts.weather import get_weather_description
from control import schedules

# ---------------------------------------------------------------------------
# Zone helper — for single-zone commands, use the first configured zone.
# When multi-zone Telegram support is added later, commands will accept an
# optional zone argument and resolve it here.
# ---------------------------------------------------------------------------

_zones: dict = {}   # populated by create_bot()


def _default_zone():
    """Return the Zone instance for the first configured zone."""
    zone_id = config.get_first_zone_id()
    return _zones[zone_id]

logger = logging.getLogger(__name__)

# Injected by main.py at startup (see create_bot)
_assistant = None


# =============================================================================
# SECTION 1: AUTHORIZATION
# =============================================================================

def is_allowed(user_id: int) -> bool:
    """
    Check if a Telegram user is allowed to interact with the bot.
    If ALLOWED_USERS is empty, all users are allowed.
    Handles both list of ints (config.py) and list of strings (legacy .env).
    """
    allowed = config.ALLOWED_USERS
    if not allowed:
        return True
    return user_id in allowed or str(user_id) in [str(a) for a in allowed]


# =============================================================================
# SECTION 2: GENERAL COMMANDS
# /status, /set, /help
# =============================================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status - Show current system state.
    """
    if not is_allowed(update.effective_user.id):
        return

    zone      = _default_zone()
    thermostat = state.get_device(zone.thermostat_name or "")
    radiator   = state.get_derived(f"{zone.id}_radiator_output")
    weather_data = state.get_weather()

    msg = f"🌡️ <b>{zone.display_name} — Thermostat</b>\n"
    msg += f"Current: <code>{thermostat.get('local_temperature', 'N/A')}°C</code>\n"
    msg += f"Target: <code>{thermostat.get('occupied_heating_setpoint', 'N/A')}°C</code>\n\n"

    watts = radiator.get("watts", "N/A")
    msg += f"🔥 <b>Radiator Output</b>: <code>{watts} W</code>\n\n"

    msg += "☁️ <b>Outside</b>\n"
    temp = weather_data.get("temp", "N/A")
    hum = weather_data.get("hum", "N/A")
    code = weather_data.get("code")
    desc = get_weather_description(code) if code is not None else "N/A"
    wind = weather_data.get("wind_speed", "N/A")
    solar = weather_data.get("solar_ghi", "N/A")
    msg += f"{desc}\n"
    msg += f"<code>{temp}°C</code> | <code>{hum}%</code> | <code>{wind} km/h</code>\n"
    msg += f"☀️ Solar: <code>{solar} W/m²</code>\n\n"

    msg += "📊 <b>Sensors</b>\n"
    sensor_roles = ["room_temp", "supply_temp", "return_temp"]
    for role in sensor_roles:
        device_name = zone.get_device_name(role)
        if device_name:
            sensor_data = state.get_device(device_name)
            temp_val = sensor_data.get("temperature", "N/A")
            role_label = role.replace("_", " ").title()
            msg += f"• {role_label}: <code>{temp_val}°C</code>\n"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /set <temperature> - Set thermostat target temperature.
    """
    if not is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: <code>/set 21.5</code>",
            parse_mode="HTML"
        )
        return

    try:
        target_temp = float(context.args[0])
        if not (5 <= target_temp <= 30):
            await update.message.reply_text("❌ Temperature must be between 5°C and 30°C")
            return

        thermostat_name = _default_zone().thermostat_name
        if thermostat_name:
            mqtt.send_command(thermostat_name, {"occupied_heating_setpoint": target_temp})
            await update.message.reply_text(
                f"✅ Target set to <b>{target_temp}°C</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ No thermostat configured")

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid temperature. Usage: <code>/set 21.5</code>",
            parse_mode="HTML"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help - Show available commands.
    """
    if not is_allowed(update.effective_user.id):
        return

    msg = "🏠 <b>TroubleHouseBot Commands</b>\n\n"
    msg += "<b>General</b>\n"
    msg += "/status - Current temperatures and radiator output\n"
    msg += "/set - Set thermostat target (e.g., /set 21.5)\n"
    msg += "/help - Show this help message\n"
    msg += "\n<b>Monitoring</b>\n"
    msg += "/weather - Detailed weather data\n"
    msg += "/price - Current electricity price\n"
    msg += "/schedule - Occupancy forecast (e.g., /schedule 48)\n"
    msg += "\n<b>MPC</b>\n"
    msg += "/mpc - MPC status and forecast chart\n"
    msg += "/mpc run - Force immediate MPC optimization\n"
    msg += "\n<b>System Identification</b>\n"
    msg += "/sysid - Fetch and plot ID data (e.g., /sysid 72 30)\n"
    msg += "/sysid test - Run model validation tests\n"
    from sysid.models import REGISTRY
    _models = "|".join(REGISTRY.keys())
    msg += f"/sysid run [{_models}] [hours] [dt] - Run PEM estimation\n"
    msg += "/sysid models - List available model structures\n"
    msg += "/sysid accept - Apply last result to model_params.json\n"
    msg += "/sysid reject - Discard last result\n"
    msg += "\n<b>AI Assistant</b>\n"
    msg += "/chat - Start AI assistant chat\n"
    msg += "/exit - Exit AI assistant chat\n"

    await update.message.reply_text(msg, parse_mode="HTML")


# =============================================================================
# SECTION 3: MONITORING COMMANDS
# /weather, /price, /schedule
# =============================================================================

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /price - Show current electricity price.
    """
    if not is_allowed(update.effective_user.id):
        return

    p = state.get_price()

    if not p:
        await update.message.reply_text("❌ No price data available yet")
        return

    from datetime import datetime
    hour = datetime.now().hour
    if hour in [17, 18, 19]:
        period = "🔴 PEAK"
    elif hour in [0, 1, 2, 3, 4, 5]:
        period = "🟢 Low"
    else:
        period = "🟡 High"

    msg = f"💰 <b>Electricity Price</b> ({p.get('area', 'DK1')})\n\n"
    msg += f"Spot: <code>{p.get('price_spot', 'N/A'):.2f}</code> DKK/kWh\n"
    msg += f"Grid tariff: <code>{p.get('grid_tariff', 'N/A'):.2f}</code> DKK/kWh\n"
    msg += f"Full (incl. VAT): <code>{p.get('price_full', 'N/A'):.2f}</code> DKK/kWh\n\n"
    msg += f"Period: {period}"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /schedule [hours] - Show occupancy schedule and setpoints.
    """
    if not update.message:
        return
    if not is_allowed(update.effective_user.id):
        return

    hours = 24
    if context.args:
        try:
            hours = max(1, min(int(context.args[0]), 168))
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid hours. Usage: <code>/schedule 24</code>",
                parse_mode="HTML"
            )
            return

    zone_id = config.get_first_zone_id()
    current = schedules.get_current_setpoint(room=zone_id)
    if not current:
        await update.message.reply_text("❌ No schedule configured")
        return

    horizon = schedules.get_setpoint_horizon(hours, room=zone_id)
    if not horizon:
        await update.message.reply_text("❌ Failed to get schedule forecast")
        return

    msg = "📅 <b>Schedule Forecast</b>\n\n"

    state_emoji = "🏠" if current["state"] == "occupied" else "🌙"
    source_info = " (special day)" if current["source"] == "special_day" else ""
    msg += f"<b>Now:</b> {state_emoji} {current['setpoint']}°C ({current['state']}{source_info})\n\n"

    temps = schedules.get_setpoint_temperatures()
    msg += f"<b>Setpoints:</b> 🏠 {temps.get('occupied', '?')}°C | 🌙 {temps.get('unoccupied', '?')}°C\n\n"
    msg += f"<b>Next {hours} hours:</b>\n"

    from datetime import datetime
    current_date = None
    day_ranges = []
    range_start = None
    range_state = None
    prev_hour = None

    for i in range(horizon["n_steps"]):
        t = horizon["time"][i]
        state_val = horizon["state"][i]
        date_str = t.strftime("%a %d/%m")

        if date_str != current_date:
            if range_start is not None and current_date is not None:
                day_ranges.append((current_date, range_start, prev_hour, range_state))
            current_date = date_str
            range_start = t.hour
            range_state = state_val
        elif state_val != range_state:
            day_ranges.append((current_date, range_start, prev_hour, range_state))
            range_start = t.hour
            range_state = state_val

        prev_hour = t.hour

    if range_start is not None and current_date is not None:
        day_ranges.append((current_date, range_start, prev_hour, range_state))

    prev_day = None
    for day, start, end, state_val in day_ranges:
        emoji = "🏠" if state_val == "occupied" else "🌙"
        if day != prev_day:
            if prev_day is not None:
                msg += "\n"
            msg += f"<b>{day}:</b> "
            prev_day = day
        else:
            msg += " | "
        if start == end:
            msg += f"{emoji}{start:02d}"
        else:
            msg += f"{emoji}{start:02d}-{end+1:02d}"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /weather - Show detailed weather information.
    """
    if not is_allowed(update.effective_user.id):
        return

    w = state.get_weather()
    if not w:
        await update.message.reply_text("❌ No weather data available yet")
        return

    code = w.get("code")
    desc = get_weather_description(code) if code is not None else "Unknown"
    is_day = "☀️" if w.get("is_day") else "🌙"

    msg = f"{is_day} <b>{desc}</b>\n\n"
    msg += "📊 <b>Basic</b>\n"
    msg += f"Temperature: <code>{w.get('temp', 'N/A')}°C</code>\n"
    msg += f"Humidity: <code>{w.get('hum', 'N/A')}%</code>\n"
    msg += f"Pressure: <code>{w.get('pressure', 'N/A')} hPa</code>\n"
    msg += f"Rain: <code>{w.get('rain', 'N/A')} mm</code>\n\n"

    wind_dir = w.get('wind_dir')
    from forecasts.weather import get_wind_direction_name
    dir_name = get_wind_direction_name(wind_dir)

    msg += "💨 <b>Wind</b>\n"
    msg += f"Speed: <code>{w.get('wind_speed', 'N/A')} km/h</code>\n"
    msg += f"Direction: <code>{wind_dir}° ({dir_name})</code>\n"
    msg += f"Gusts: <code>{w.get('wind_gust', 'N/A')} km/h</code>\n\n"

    msg += "☀️ <b>Solar - Horizontal</b> (W/m²)\n"
    msg += f"GHI: <code>{w.get('solar_ghi', 'N/A')}</code> | "
    msg += f"Direct: <code>{w.get('solar_direct', 'N/A')}</code> | "
    msg += f"Diffuse: <code>{w.get('solar_diffuse', 'N/A')}</code>\n\n"

    msg += "🧱 <b>Solar - Walls</b> (W/m²)\n"
    for surface in config.SOLAR_SURFACES:
        name = surface["name"]
        value = w.get(f"solar_{name}", "N/A")
        msg += f"{name.capitalize()}: <code>{value}</code>  "
    msg += "\n\n"

    msg += "🌱 <b>Soil Temperature</b>\n"
    msg += f"Surface: <code>{w.get('soil_0cm', 'N/A')}°C</code> | "
    msg += f"18cm: <code>{w.get('soil_18cm', 'N/A')}°C</code> | "
    msg += f"54cm: <code>{w.get('soil_54cm', 'N/A')}°C</code>"

    await update.message.reply_text(msg, parse_mode="HTML")


# =============================================================================
# SECTION 4: MPC COMMANDS
# /mpc
# =============================================================================

async def cmd_mpc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mpc [run] - Show MPC status and latest optimization plot.
    """
    if not update.message:
        return
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized")
        return

    from control import mpc as mpc_module

    args = context.args
    force_run = args and args[0].lower() == "run"

    if force_run:
        await update.message.reply_text("⚡ Running MPC optimization...")
        try:
            from control.radiator import Radiator, supply_temp_from_outdoor
            zone      = _default_zone()
            weather   = state.get_weather()
            t_outdoor = weather.get("temp", 0.0) if weather else 0.0
            t_supply  = supply_temp_from_outdoor(t_outdoor)
            room_device = zone.get_device_name("room_temp")
            t_room    = state.get_device(room_device).get("temperature") if room_device else 20.0
            t_room    = t_room or 20.0
            if zone.radiator:
                max_heat = zone.radiator.max_output(t_supply=t_supply, t_room=t_room)
            else:
                max_heat = 1000.0
            setpoint = mpc_module.run_mpc_step(zone_id=zone.id, max_heat=max_heat)
            if setpoint is None:
                await update.message.reply_text("❌ MPC optimization failed")
                return
        except Exception as e:
            await update.message.reply_text(f"❌ MPC error: {e}")
            return

    try:
        zone_id = config.get_first_zone_id()
        status = mpc_module.get_mpc_status(zone_id)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to get MPC status: {e}")
        return

    msg = "🎯 <b>MPC Status</b>\n\n"
    if status.get("last_update"):
        msg += f"Last run: <code>{status['last_update'][:19]}</code>\n"
    else:
        msg += "Last run: <code>Never</code>\n"

    if status.get("last_y_measured") is not None:
        msg += f"T_measured: <code>{status['last_y_measured']:.1f}°C</code>\n"

    x_hat = status.get("x_hat", [])
    if len(x_hat) >= 2:
        msg += f"State estimate: Ti=<code>{x_hat[0]:.1f}°C</code>, Tm=<code>{x_hat[1]:.1f}°C</code>\n"

    msg += f"\nHorizon: <code>{status['horizon_hours']}h</code> @ <code>{status['dt_minutes']}min</code>\n"

    await update.message.reply_text(msg, parse_mode="HTML")

    chart_path = mpc_module.get_mpc_plot_path(config.get_first_zone_id())
    if chart_path:
        try:
            import os
            filename = os.path.basename(chart_path)
            with open(chart_path, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename=filename,
                    caption="📊 MPC forecast — open in browser for interactive view",
                )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not send chart: {e}")
    else:
        await update.message.reply_text("📊 No MPC chart available yet. Use /mpc run to generate one.")


# =============================================================================
# SECTION 5: SYSTEM IDENTIFICATION COMMANDS
# /sysid
# =============================================================================

_pending_sysid: dict = {}

async def cmd_sysid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sysid [hours] - Fetch and plot identification data for inspection.

    Usage:
        /sysid       — plot last 48 hours
        /sysid 72    — plot last 72 hours
    """
    if not update.message:
        return
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized")
        return

    # Parse subcommand and arguments
    # Usage:
    #   /sysid [hours] [dt]        — fetch and plot data
    #   /sysid test [hours] [dt]   — run model validation tests
    #   /sysid run [hours] [dt]    — run full PEM estimation
    #   /sysid accept              — apply last result to config
    #   /sysid reject              — discard last result

    args = context.args or []
    sub  = args[0].lower() if args else "plot"

    # --- Accept / Reject ---
    if sub == "accept":
        res = _pending_sysid.get("result")
        if not res:
            await update.message.reply_text("⚠️ No pending result. Run /sysid run first.")
            return
        from sysid.runner import apply_result
        ok = apply_result(res)
        _pending_sysid.clear()
        if ok:
            await update.message.reply_text(
                "✅ model_params.json updated.\n"
                "New parameters will be used on the next MPC step."
            )
        else:
            await update.message.reply_text("❌ Failed to apply parameters.")
        return

    if sub == "reject":
        _pending_sysid.clear()
        await update.message.reply_text("🗑️ Result discarded.")
        return

    # --- List available model structures ---
    if sub == "models":
        from sysid.models import list_models
        await update.message.reply_text(
            "<b>Available model structures:</b>\n<pre>" + list_models() + "</pre>",
            parse_mode="HTML"
        )
        return

    # --- Parse model name (run only), hours, dt ---
    from sysid.models import REGISTRY
    model_name = None
    arg_offset = 1 if sub in ("test", "run") else 0

    # Check if first positional arg after subcommand is a model name
    if sub == "run" and len(args) > arg_offset:
        candidate = args[arg_offset]
        # Match case-insensitively against registry keys
        candidate_match = next((k for k in REGISTRY if k.lower() == candidate.lower()), None)
        if candidate_match:
            model_name = candidate_match
            arg_offset += 1

    hours      = 72.0
    dt_minutes = None
    fixed_params = []   # parameters to fix (not estimated)

    # Parse --fix param1,param2 anywhere in remaining args
    remaining = args[arg_offset:]
    positional = []
    i = 0
    while i < len(remaining):
        if remaining[i] == "--fix" and i + 1 < len(remaining):
            fixed_params = [p.strip() for p in remaining[i+1].split(",")]
            i += 2
        else:
            positional.append(remaining[i])
            i += 1

    try:
        if len(positional) > 0:
            hours = float(positional[0])
        if len(positional) > 1:
            dt_minutes = int(positional[1])
    except ValueError:
        await update.message.reply_text(
            f"Usage: /sysid run [{'|'.join(__import__('sysid.models', fromlist=['REGISTRY']).REGISTRY.keys())}] [hours] [dt_minutes] [--fix param1,param2]"
        )
        return

    dt_str = f"{dt_minutes} min" if dt_minutes else "default dt"
    await update.message.reply_text(f"🔬 Fetching {hours:.0f}h of ID data ({dt_str})...")

    try:
        import asyncio
        from sysid.data import fetch_id_data
        from sysid.plot import plot_id_data

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: fetch_id_data(hours_back=hours, dt_minutes=dt_minutes)
        )

        if data is None:
            await update.message.reply_text("❌ Failed to fetch data from InfluxDB")
            return

        # Always show data summary
        msg = (
            f"📊 <b>ID Data ({hours:.0f}h)</b>\n"
            f"<code>{data.summary()}</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

        if sub == "test":
            # --- Model validation tests ---
            await update.message.reply_text("🧪 Running model tests...")
            from sysid.test import run_tests
            report = await loop.run_in_executor(None, lambda: run_tests(data))
            await update.message.reply_text(report, parse_mode="HTML")

        elif sub == "run":
            # --- Full PEM estimation ---
            await update.message.reply_text("⚙️ Running PEM estimation (this may take a while)...")
            from sysid.runner import run_identification
            _model = model_name
            _fixed = fixed_params
            run_result = await loop.run_in_executor(
                None,
                lambda: run_identification(
                    hours_back=hours,
                    dt_minutes=dt_minutes,
                    model_name=_model,
                    fixed_params=_fixed,
                ),
            )
            _pending_sysid["result"] = run_result

            # Text summary
            await update.message.reply_text(
                run_result.telegram_summary(), parse_mode="HTML"
            )

            # Results plot
            if run_result.estimation is not None:
                from sysid.results_plot import plot_results
                filepath, err = plot_results(data, run_result.estimation)
                if filepath:
                    import os
                    with open(filepath, "rb") as f:
                        await update.message.reply_document(
                            document=f,
                            filename=os.path.basename(filepath),
                            caption="📊 Open in browser to inspect fit",
                        )
                    os.unlink(filepath)
                else:
                    await update.message.reply_text(f"⚠️ Plot failed: {err}")

        else:
            # --- Default: data inspection plot ---
            filepath, err = plot_id_data(data)
            if filepath is None:
                await update.message.reply_text(f"⚠️ Plot failed: {err}")
                return
            import os
            with open(filepath, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(filepath),
                    caption="📊 Open in browser to inspect ID data",
                )
            os.unlink(filepath)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# =============================================================================
# SECTION 6: AI ASSISTANT COMMANDS
# /chat, /exit, /plot
# =============================================================================

async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /chat - Enter AI assistant chat mode.
    """
    if not is_allowed(update.effective_user.id):
        return

    reply = _assistant.enter_chat_mode(update.effective_chat.id)
    await update.message.reply_text(reply, parse_mode="HTML")


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /exit - Exit AI assistant chat mode.
    """
    if not is_allowed(update.effective_user.id):
        return

    reply = _assistant.exit_chat_mode(update.effective_chat.id)
    await update.message.reply_text(reply)


async def _send_chart(update: Update, path: str, title: str) -> None:
    """Send a Plotly HTML chart as a Telegram document, then delete the temp file."""
    try:
        filename = title.replace(" ", "_").replace("|", "-") + ".html"
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=f"📊 {title}",
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def cmd_plot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /plot <type> [range] - Generate a historical data chart.

    Examples:
        /plot temperatures 24h
        /plot prices 7d
        /plot weather yesterday
    """
    if not is_allowed(update.effective_user.id):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /plot <type> [range]\n\n"
            "Types: temperatures, prices, weather\n"
            "Ranges: 6h, 24h, 7d, 30d, today, yesterday, this week, last week",
        )
        return

    chart_type = args[0].lower()
    range_str  = " ".join(args[1:]) if len(args) > 1 else "24h"

    await update.message.reply_text(f"📊 Generating {chart_type} chart for {range_str}...")

    from plots.history import generate_chart
    path, title = generate_chart(chart_type, range_str)

    if path is None:
        await update.message.reply_text(f"❌ {title}")
        return

    await _send_chart(update, path, title)


def _strip_unknown_html(text: str) -> str:
    """Remove HTML tags not supported by Telegram's parse_mode=HTML."""
    import re
    allowed = {"b", "i", "u", "s", "code", "pre", "a"}
    def replace(m):
        tag = m.group(1).lstrip("/").split()[0].lower()
        return m.group(0) if tag in allowed else ""
    return re.sub(r"<(/?\w+[^>]*)>", replace, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle plain text messages (not commands).
    Forwards to AI assistant if in chat mode, otherwise ignores.
    """
    if not is_allowed(update.effective_user.id):
        return

    response = await _assistant.handle_message(
        update.effective_chat.id,
        update.message.text,
    )

    if response is None:
        return

    # Send any charts first (they provide context for the text reply)
    for path in response.documents:
        title = os.path.basename(path).replace(".html", "").replace("_", " ")
        await _send_chart(update, path, title)

    if response.text:
        text = _strip_unknown_html(response.text)
        await update.message.reply_text(text, parse_mode="HTML")


# =============================================================================
# SECTION 7: BOT SETUP
# =============================================================================

def create_bot(assistant, zones: dict) -> Application:
    """
    Create and configure the Telegram bot application.

    Args:
        assistant: AIAssistant instance from main.py

    Returns:
        Configured Application instance ready to run.
    """
    global _assistant, _zones
    _assistant = assistant
    _zones     = zones

    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN not configured in environment")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Existing command handlers
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("mpc", cmd_mpc))
    app.add_handler(CommandHandler("sysid", cmd_sysid))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # AI assistant commands
    app.add_handler(CommandHandler("plot", cmd_plot))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("exit", cmd_exit))

    # Plain text handler (for AI chat mode)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Telegram bot configured")
    return app