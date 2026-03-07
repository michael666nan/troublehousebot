# =============================================================================
# TOOL DEFINITIONS
# =============================================================================
# JSON schemas sent to the Claude API describing available tools.
#
# Adding a new data type to get_home_data:
#   1. Add the new type to the enum list below
#   2. Add its _fetch_<type>() function in handlers.py
#   3. Register it in _DATA_HANDLERS in handlers.py
#
# Adding a new schedule action to update_schedule:
#   1. Add the action name to the enum below
#   2. Document its parameters in the description
#   3. Add its handler in _SCHEDULE_ACTIONS in handlers.py
# =============================================================================

TOOL_DEFINITIONS = [
    # =========================================================================
    # READ TOOLS
    # =========================================================================
    {
        "name": "get_home_data",
        "description": (
            "Fetch live data from the home system. "
            "Pass a list of data types to retrieve everything needed in one call. "
            "Only include 'schedule' when the user is adjusting specific existing hours. Do NOT fetch 'schedule' when the user wants to create or redesign a schedule — in that case propose one directly without fetching."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_type": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "temperatures",  # Room, supply, return, outdoor temps + setpoint
                            "prices",        # Spot price, grid tariff, total in DKK/kWh
                            "weather",       # Outdoor conditions, wind, solar irradiance
                            "mpc_status",    # Last run, state estimate, Kalman innovation
                            "schedule",      # Current setpoint, weekly schedule, special days
                        ],
                    },
                    "description": "List of data types to fetch in one call.",
                },
            },
            "required": ["data_type"],
        },
    },

    # =========================================================================
    # WRITE TOOLS
    # =========================================================================
    {
        "name": "update_schedule",
        "description": (
            "Modify the heating schedule. "
            "IMPORTANT: Only fetch current schedule data if adjusting specific existing hours. "
            "and wait for explicit confirmation before calling this tool. "
            "Never call this tool without user confirmation in the current message. "
            "\n\nActions and their required parameters:"
            "\n  set_temperature  — change occupied/unoccupied comfort bounds"
            "\n    state: 'occupied' or 'unoccupied'"
            "\n    t_min: float in °C — lower comfort bound (setpoint). Omit to leave unchanged."
            "\n    t_max: float in °C — upper comfort bound. Omit to leave unchanged."
            "\n  set_hours        — set a range of hours on a specific weekday"
            "\n    day: 'monday'..'sunday'"
            "\n    start_hour: int 0-23"
            "\n    end_hour: int 0-23 (inclusive)"
            "\n    occupied: bool"
            "\n    room: str (default: 'default')"
            "\n  copy_day         — copy one weekday's schedule to another"
            "\n    from_day: 'monday'..'sunday'"
            "\n    to_day: 'monday'..'sunday'"
            "\n    room: str (default: 'default')"
            "\n  set_special_day  — override a specific date"
            "\n    date: 'YYYY-MM-DD'"
            "\n    special_type: 'away' (unoccupied all day) or 'home' (occupied all day)"
            "\n                  or 'custom' (use start_hour + end_hour)"
            "\n    start_hour: int (only for custom)"
            "\n    end_hour: int (only for custom)"
            "\n    room: str (default: 'default')"
            "\n  remove_special_day — revert a date to the weekly schedule"
            "\n    date: 'YYYY-MM-DD'"
            "\n    room: str (default: 'default')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "set_temperature",
                        "set_hours",
                        "set_weekly_pattern",
                        "copy_day",
                        "set_special_day",
                        "remove_special_day",
                    ],
                    "description": (
                        "The schedule action to perform. "
                        "Use set_weekly_pattern to set a full weekly schedule in one call — "
                        "prefer this over multiple set_hours calls when changing multiple days."
                    ),
                },
                "state": {
                    "type": "string",
                    "enum": ["occupied", "unoccupied"],
                    "description": "For set_temperature: which state to update.",
                },
                "t_min": {
                    "type": "number",
                    "description": "For set_temperature: lower comfort bound (setpoint) in °C.",
                },
                "t_max": {
                    "type": "number",
                    "description": "For set_temperature: upper comfort bound in °C (must be >= t_min).",
                },
                "day": {
                    "type": "string",
                    "description": "For set_hours: day of week (e.g. 'monday').",
                },
                "start_hour": {
                    "type": "integer",
                    "description": "For set_hours / set_special_day custom: start hour (0-23).",
                },
                "end_hour": {
                    "type": "integer",
                    "description": "For set_hours / set_special_day custom: end hour (0-23, inclusive).",
                },
                "occupied": {
                    "type": "boolean",
                    "description": "For set_hours: true = occupied, false = unoccupied.",
                },
                "from_day": {
                    "type": "string",
                    "description": "For copy_day: source day.",
                },
                "to_day": {
                    "type": "string",
                    "description": "For copy_day: destination day.",
                },
                "date": {
                    "type": "string",
                    "description": "For set_special_day / remove_special_day: date as 'YYYY-MM-DD'.",
                },
                "special_type": {
                    "type": "string",
                    "enum": ["away", "home", "custom"],
                    "description": "For set_special_day: type of override.",
                },
                "room": {
                    "type": "string",
                    "description": "Room name (default: 'default').",
                },
                "patterns": {
                    "type": "array",
                    "description": (
                        "For set_weekly_pattern: list of patterns, each with "
                        "'days' (list of day names) and 'occupied_ranges' "
                        "(list of [start_hour, end_hour] pairs, inclusive). "
                        "All other hours are set to unoccupied automatically. "
                        "Example: [{'days': ['monday','tuesday'], 'occupied_ranges': [[6,6],[21,23]]}]"
                    ),
                },
            },
            "required": ["action"],
        },
    },

    # =========================================================================
    # CHART TOOLS
    # =========================================================================
    {
        "name": "plot_history",
        "description": (
            "Generate an interactive historical chart from InfluxDB data and send it to the user. "
            "Use when the user asks to see, plot, or chart historical data. "
            "The chart opens in the phone browser with zoom and hover support."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["temperatures", "prices", "weather"],
                    "description": (
                        "temperatures: room, supply, return, outdoor temps, setpoint, radiator output. "
                        "prices: spot and full electricity price with tariff period shading. "
                        "weather: outdoor temperature, wind speed, solar irradiance."
                    ),
                },
                "time_range": {
                    "type": "string",
                    "description": (
                        "Time range for the chart. Supports: "
                        "'24h', '48h', '7d', '30d' (relative), "
                        "'today', 'yesterday', 'this week', 'last week', "
                        "'YYYY-MM-DD' (specific day), "
                        "'YYYY-MM-DD to YYYY-MM-DD' (date range)."
                    ),
                },
            },
            "required": ["chart_type", "time_range"],
        },
    },

    # =========================================================================
    # MEMORY TOOLS
    # =========================================================================
    {
        "name": "search_memory",
        "description": (
            "Search past conversation history by keyword and optional date range. "
            "Use when the user references something from a previous session, "
            "asks 'do you remember...', 'last time...', 'what did we discuss...'. "
            "Leave query empty to match all messages in a date range. "
            "Returns matching messages with timestamps, newest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords to search for, comma-separated. "
                        "Provide multiple variations for better coverage, e.g. "
                        "'temperature, temp, temperatur'. "
                        "Matches any message containing at least one keyword. "
                        "Use empty string '' to match all messages (useful with date filters)."
                    ),
                },
                "date_filter": {
                    "type": "string",
                    "description": (
                        "Optional date filter. Supports: "
                        "'today', 'yesterday', 'last week', 'last N days', 'YYYY-MM-DD'."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_recent_history",
        "description": (
            "Retrieve the last N messages from conversation history without keyword filtering. "
            "Use when the user asks 'what did we talk about?', 'remind me of our last conversation'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of recent messages to retrieve (default 10, max 50).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_tool_stats",
        "description": (
            "Return statistics about tool usage — how many times each tool has been called. "
            "Use when asked about system performance, tool usage patterns, or analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # =========================================================================
    # INFLUX TOOLS
    # =========================================================================
    {
        "name": "get_influx_schema",
        "description": (
            "Discover what data is available in InfluxDB. "
            "Returns all measurements, their field names, and the time range of stored data. "
            "Call this first when the user asks what data is available, or before using "
            "plot_influx to verify measurement and field names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "plot_influx",
        "description": (
            "Generate an ad-hoc interactive chart from any InfluxDB fields. "
            "Use get_influx_schema first to discover valid measurement and field names. "
            "Multiple series can be plotted together. Use axis=2 to put a series on a "
            "secondary y-axis when units differ (e.g. temperature + watts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "series": {
                    "type": "array",
                    "description": "List of series to plot.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "measurement": {
                                "type": "string",
                                "description": "InfluxDB measurement name (from get_influx_schema).",
                            },
                            "field": {
                                "type": "string",
                                "description": "Field key within the measurement.",
                            },
                            "label": {
                                "type": "string",
                                "description": "Legend label for this series (optional).",
                            },
                            "unit": {
                                "type": "string",
                                "description": "Unit string for y-axis label e.g. \"°C\", \"W\", \"DKK/kWh\" (optional).",
                            },
                            "axis": {
                                "type": "integer",
                                "enum": [1, 2],
                                "description": "Y-axis: 1 = primary (default), 2 = secondary.",
                            },
                        },
                        "required": ["measurement", "field"],
                    },
                },
                "time_range": {
                    "type": "string",
                    "description": (
                        "Time range for the chart. Supports: "
                        "\"24h\", \"48h\", \"7d\", \"30d\" (relative), "
                        "\"today\", \"yesterday\", \"this week\", \"last week\", "
                        "\"YYYY-MM-DD\" (specific day), "
                        "\"YYYY-MM-DD to YYYY-MM-DD\" (date range)."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Chart title (optional).",
                },
            },
            "required": ["series", "time_range"],
        },
    },


]