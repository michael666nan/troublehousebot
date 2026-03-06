# =============================================================================
# TOOL HANDLERS
# =============================================================================
# Functions executed when Claude calls a tool.
#
# Structure:
#   Section 1: Data fetchers (used by get_home_data)
#   Section 2: Schedule action handlers (used by update_schedule)
#   Section 3: Top-level tool handlers (called by dispatcher)
#   Section 4: Dispatcher
#
# Rules:
#   - Fetchers and memory tools only READ
#   - Schedule actions WRITE — always called after user confirmation
#   - No Telegram or LLM knowledge here
# =============================================================================

import logging
from datetime import datetime

from control import schedules as schedules_module
from .. import memory as memory_module
from ..memory import parse_date_query

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: DATA FETCHERS (read-only, used by get_home_data)
# =============================================================================

def _fetch_temperatures(state) -> dict:
    thermostat = state.get_by_role("thermostat")
    room       = state.get_by_role("room_temp")
    supply     = state.get_by_role("supply_temp")
    ret        = state.get_by_role("return_temp")
    weather    = state.get_weather()
    radiator   = state.get_derived("radiator_1_output")

    return {
        "room_temperature_c":        room.get("temperature"),
        "supply_pipe_temperature_c": supply.get("temperature"),
        "return_pipe_temperature_c": ret.get("temperature"),
        "outdoor_temperature_c":     weather.get("temp"),
        "thermostat_setpoint_c":     thermostat.get("occupied_heating_setpoint"),
        "thermostat_measured_c":     thermostat.get("local_temperature"),
        "radiator_output_w":         radiator.get("watts"),
    }


def _fetch_prices(state) -> dict:
    from prices import get_current_period
    price = state.get_price()
    if not price:
        return {"error": "No price data available yet"}

    return {
        "spot_price_dkk_per_kwh":  price.get("price_spot"),
        "grid_tariff_dkk_per_kwh": price.get("grid_tariff"),
        "total_price_dkk_per_kwh": price.get("price_full"),
        "area":                    price.get("area", "DK1"),
        "time_of_use_period":      get_current_period(),
    }


def _fetch_weather(state) -> dict:
    w = state.get_weather()
    if not w:
        return {"error": "No weather data available yet"}

    return {
        "temperature_c":      w.get("temp"),
        "humidity_pct":       w.get("hum"),
        "pressure_hpa":       w.get("pressure"),
        "wind_speed_kmh":     w.get("wind_speed"),
        "wind_direction_deg": w.get("wind_dir"),
        "wind_gusts_kmh":     w.get("wind_gust"),
        "rain_mm":            w.get("rain"),
        "solar_ghi_w_m2":     w.get("solar_ghi"),
        "solar_direct_w_m2":  w.get("solar_direct"),
        "is_daytime":         w.get("is_day"),
    }


def _fetch_mpc_status(state) -> dict:
    try:
        import mpc as mpc_module
        status = mpc_module.get_mpc_status()
    except Exception as e:
        logger.warning(f"Could not get MPC status: {e}")
        return {"error": f"MPC status unavailable: {e}"}

    x_hat = status.get("x_hat", [])
    return {
        "last_run":               status.get("last_update"),
        "indoor_temp_estimate_c": x_hat[0] if len(x_hat) > 0 else None,
        "mass_temp_estimate_c":   x_hat[1] if len(x_hat) > 1 else None,
        "kalman_innovation_c":    status.get("innovation"),
        "horizon_hours":          status.get("horizon_hours"),
        "timestep_minutes":       status.get("dt_minutes"),
        "max_heat_w":             status.get("max_heat"),
    }


def _fetch_schedule(state) -> dict:
    """
    Return a full schedule overview:
    - Current setpoint and state
    - Occupied/unoccupied temperatures
    - Weekly schedule (summarised as occupied hours per day)
    - Upcoming special days (next 30 days)
    """
    current    = schedules_module.get_current_setpoint()
    temps      = schedules_module.get_setpoint_temperatures()
    weekly     = schedules_module.get_weekly_schedule()
    special    = schedules_module.get_special_days() or {}

    # Summarise weekly schedule as occupied hours per day (more readable for Claude)
    weekly_summary = {}
    if weekly:
        for day, states in weekly.items():
            occupied = [h for h, s in enumerate(states) if s == "occupied"]
            weekly_summary[day] = occupied

    # Only show special days in the next 30 days
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    upcoming_special = {
        date: (
            "away (unoccupied all day)"
            if all(s == "unoccupied" for s in states)
            else "home (occupied all day)"
            if all(s == "occupied" for s in states)
            else f"custom ({sum(1 for s in states if s == 'occupied')} occupied hours)"
        )
        for date, states in sorted(special.items())
        if today <= date <= cutoff
    }

    return {
        "current_setpoint_c": current.get("setpoint") if current else None,
        "current_state":      current.get("state") if current else None,
        "current_day":        current.get("day") if current else None,
        "current_hour":       current.get("hour") if current else None,
        "temperatures": {
            "occupied_T_min_c":   temps.get("occupied",   {}).get("T_min") if isinstance(temps.get("occupied"), dict) else temps.get("occupied"),
            "occupied_T_max_c":   temps.get("occupied",   {}).get("T_max") if isinstance(temps.get("occupied"), dict) else None,
            "unoccupied_T_min_c": temps.get("unoccupied", {}).get("T_min") if isinstance(temps.get("unoccupied"), dict) else temps.get("unoccupied"),
            "unoccupied_T_max_c": temps.get("unoccupied", {}).get("T_max") if isinstance(temps.get("unoccupied"), dict) else None,
        },
        "weekly_occupied_hours": weekly_summary,
        "upcoming_special_days": upcoming_special,
    }


# Registry: data_type string → fetcher function
_DATA_HANDLERS = {
    "temperatures": _fetch_temperatures,
    "prices":       _fetch_prices,
    "weather":      _fetch_weather,
    "mpc_status":   _fetch_mpc_status,
    "schedule":     _fetch_schedule,
}


# =============================================================================
# SECTION 2: SCHEDULE ACTIONS (write, called after user confirmation)
# =============================================================================

def _action_set_temperature(tool_input: dict) -> dict:
    """Change T_min and/or T_max for occupied or unoccupied state."""
    state = tool_input.get("state")
    t_min = tool_input.get("t_min")
    t_max = tool_input.get("t_max")

    if not state:
        return {"error": "state is required"}
    if t_min is None and t_max is None:
        return {"error": "at least one of t_min or t_max is required"}
    if t_min is not None and t_max is not None and float(t_max) < float(t_min):
        return {"error": f"t_max ({t_max}) must be >= t_min ({t_min})"}

    messages = []
    if t_min is not None:
        if not schedules_module.set_setpoint_temperature(state, float(t_min)):
            return {"error": f"Failed to update {state} T_min"}
        messages.append(f"T_min={t_min}°C")
    if t_max is not None:
        if not schedules_module.set_setpoint_t_max(state, float(t_max)):
            return {"error": f"Failed to update {state} T_max"}
        messages.append(f"T_max={t_max}°C")

    return {"success": True, "message": f"Set {state} {', '.join(messages)}"}


def _action_set_hours(tool_input: dict) -> dict:
    """Set a range of hours on a specific weekday."""
    day        = tool_input.get("day")
    start_hour = tool_input.get("start_hour")
    end_hour   = tool_input.get("end_hour")
    occupied   = tool_input.get("occupied", True)
    room       = tool_input.get("room", "default")

    if not day or start_hour is None or end_hour is None:
        return {"error": "day, start_hour, and end_hour are required"}

    state = "occupied" if occupied else "unoccupied"
    success = schedules_module.set_weekly_range(day, start_hour, end_hour, state, room)

    if success:
        return {
            "success": True,
            "message": f"Set {day} {start_hour:02d}:00–{end_hour:02d}:00 to {state} for room '{room}'",
        }
    return {"error": f"Failed to update {day} schedule"}


def _action_copy_day(tool_input: dict) -> dict:
    """Copy one weekday's schedule to another."""
    from_day = tool_input.get("from_day")
    to_day   = tool_input.get("to_day")
    room     = tool_input.get("room", "default")

    if not from_day or not to_day:
        return {"error": "from_day and to_day are required"}

    success = schedules_module.copy_weekly_day(from_day, to_day, room)

    if success:
        return {
            "success": True,
            "message": f"Copied {from_day} schedule to {to_day} for room '{room}'",
        }
    return {"error": f"Failed to copy {from_day} to {to_day}"}


def _action_set_special_day(tool_input: dict) -> dict:
    """Override a specific date's schedule."""
    date         = tool_input.get("date")
    special_type = tool_input.get("special_type")
    room         = tool_input.get("room", "default")

    if not date or not special_type:
        return {"error": "date and special_type are required"}

    if special_type == "away":
        success = schedules_module.set_special_day_away(date, room)
        label = "unoccupied all day"

    elif special_type == "home":
        success = schedules_module.set_special_day_home(date, room)
        label = "occupied all day"

    elif special_type == "custom":
        start_hour = tool_input.get("start_hour")
        end_hour   = tool_input.get("end_hour")
        if start_hour is None or end_hour is None:
            return {"error": "start_hour and end_hour are required for custom special day"}

        # Build a 24-element state list
        states = [
            "occupied" if start_hour <= h <= end_hour else "unoccupied"
            for h in range(24)
        ]
        success = schedules_module.set_special_day(date, states, room)
        label = f"occupied {start_hour:02d}:00–{end_hour:02d}:00"

    else:
        return {"error": f"Unknown special_type: {special_type}"}

    if success:
        return {
            "success": True,
            "message": f"Set special day {date} as {label} for room '{room}'",
        }
    return {"error": f"Failed to set special day {date}"}


def _action_remove_special_day(tool_input: dict) -> dict:
    """Remove a special day override, reverting to the weekly schedule."""
    date = tool_input.get("date")
    room = tool_input.get("room", "default")

    if not date:
        return {"error": "date is required"}

    success = schedules_module.remove_special_day(date, room)

    if success:
        return {
            "success": True,
            "message": f"Removed special day {date} for room '{room}' — reverted to weekly schedule",
        }
    return {"error": f"No special day found for {date}"}


# Registry: action string → handler function
_SCHEDULE_ACTIONS = {
    "set_temperature":    _action_set_temperature,
    "set_hours":          _action_set_hours,
    "copy_day":           _action_copy_day,
    "set_special_day":    _action_set_special_day,
    "remove_special_day": _action_remove_special_day,
}


# =============================================================================
# SECTION 3: TOP-LEVEL TOOL HANDLERS
# =============================================================================

def plot_history(state, tool_input: dict, **kwargs) -> dict:
    """
    Generate a historical chart and return the file path for Telegram to send.

    Returns a dict with a special "_chart_file" key that assistant.py
    detects and sends as a Telegram document, separate from the text reply.
    """
    from plots.history import generate_chart, CHART_TYPES

    chart_type  = tool_input.get("chart_type", "temperatures")
    time_range  = tool_input.get("time_range", "24h")

    filepath, filename = generate_chart(chart_type, time_range)

    if filepath is None:
        # filename contains the error message in this case
        return {"error": filename}

    label = CHART_TYPES.get(chart_type, chart_type)
    return {
        "_chart_file": filepath,   # Detected by assistant.py
        "_filename":   filename,
        "description": f"{label} for {time_range}",
        "message":     "Chart generated — sending as file.",
    }


def get_home_data(state, tool_input: dict, **kwargs) -> dict:
    """Fetch one or more types of live home data in a single call."""
    requested = tool_input.get("data_type", [])
    if not requested:
        return {"error": "No data_type specified"}

    results = {}
    for data_type in requested:
        fetcher = _DATA_HANDLERS.get(data_type)
        if fetcher is None:
            results[data_type] = {"error": f"Unknown data type: {data_type}"}
        else:
            results[data_type] = fetcher(state)

    return results


def update_schedule(state, tool_input: dict, **kwargs) -> dict:
    """
    Apply a schedule change after user confirmation.

    The action determines which _action_* function is called.
    All actions write to schedules.json via schedules.py.
    """
    action = tool_input.get("action")
    if not action:
        return {"error": "action is required"}

    handler = _SCHEDULE_ACTIONS.get(action)
    if handler is None:
        return {"error": f"Unknown action: {action}"}

    try:
        return handler(tool_input)
    except Exception as e:
        logger.error(f"Schedule action {action} failed: {e}")
        return {"error": str(e)}



def search_memory(state, tool_input: dict, chat_id: int = 0, **kwargs) -> dict:
    query       = tool_input.get("query", "")
    date_filter = tool_input.get("date_filter", "")
    max_results = tool_input.get("max_results", 5)

    since, until = None, None
    if date_filter:
        since, until = parse_date_query(date_filter)
        if since is None and until is None:
            return {"error": f"Could not understand date filter: '{date_filter}'"}

    results = memory_module.search(
        chat_id=chat_id,
        query=query,
        max_results=max_results,
        since=since,
        until=until,
        exclude_seconds=10,
    )

    if not results:
        msg = "No past conversations found"
        if query:
            msg += f" matching '{query}'"
        if date_filter:
            msg += f" in the period '{date_filter}'"
        return {"message": msg}

    return {"query": query, "date_filter": date_filter or None, "results": results, "count": len(results)}


def get_recent_history(state, tool_input: dict, chat_id: int = 0, **kwargs) -> dict:
    n = min(tool_input.get("n", 10), 50)
    messages = memory_module.get_last_n(chat_id=chat_id, n=n, exclude_seconds=10)
    if not messages:
        return {"message": "No conversation history found yet."}
    return {"count": len(messages), "messages": messages}


def get_tool_stats(state, tool_input: dict, chat_id: int = 0, **kwargs) -> dict:
    stats = memory_module.get_tool_stats(chat_id)
    if stats["total_tool_calls"] == 0:
        return {"message": "No tool calls recorded yet."}
    sorted_tools = sorted(stats["by_tool"].items(), key=lambda x: x[1], reverse=True)
    return {
        "total_tool_calls": stats["total_tool_calls"],
        "by_tool": [{"tool": name, "calls": count} for name, count in sorted_tools],
    }



def get_influx_schema(state, tool_input: dict, **kwargs) -> dict:
    """Return all InfluxDB measurements, fields, and time ranges."""
    try:
        from plots.influx_explore import get_schema
        schema = get_schema()
        if schema.get("error"):
            return {"error": schema["error"]}
        measurements = schema.get("measurements", {})
        if not measurements:
            return {"message": "No measurements found in InfluxDB."}
        return {
            "bucket":       schema["bucket"],
            "measurements": measurements,
            "count":        len(measurements),
        }
    except Exception as e:
        logger.error(f"get_influx_schema failed: {e}")
        return {"error": str(e)}


def plot_influx(state, tool_input: dict, **kwargs) -> dict:
    """Generate an ad-hoc chart for any InfluxDB fields."""
    from plots.influx_explore import plot_fields

    series     = tool_input.get("series", [])
    time_range = tool_input.get("time_range", "24h")
    title      = tool_input.get("title", "Custom Chart")

    if not series:
        return {"error": "No series specified."}

    filepath, filename = plot_fields(series, time_range, title)

    if filepath is None:
        return {"error": filename}

    return {
        "_chart_file": filepath,
        "_filename":   filename,
        "description": f"{title} — {time_range}",
        "message":     "Chart generated — sending as file.",
    }

# =============================================================================
# SECTION 4: DISPATCHER
# =============================================================================

_HANDLERS = {
    "get_home_data":      get_home_data,
    "plot_history":       plot_history,
    "update_schedule":    update_schedule,
    "search_memory":      search_memory,
    "get_recent_history": get_recent_history,
    "get_tool_stats":     get_tool_stats,
    "get_influx_schema":  get_influx_schema,
    "plot_influx":        plot_influx,
}


def dispatch(tool_name: str, tool_input: dict, state, chat_id: int = 0) -> dict:
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        logger.warning(f"Unknown tool requested: {tool_name}")
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return handler(state, tool_input, chat_id=chat_id)
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {e}")
        return {"error": str(e)}