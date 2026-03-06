# =============================================================================
# SCHEDULES - Temperature Setpoint Schedules
# =============================================================================
#
# Manages temperature setpoints with:
#   - Two states: "occupied" and "unoccupied" (with configurable temperatures)
#   - Weekly schedules: 24 hourly states for each day of the week
#   - Special days: Override specific dates (holidays, vacations, etc.)
#   - Multi-room support: Each room can have its own schedule
#
# Data is persisted to schedules.json for easy editing and chatbot access.
#
# PRIORITY: Special day > Weekly schedule
#
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Literal

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

# Path to the schedules file (relative to this script or absolute)
SCHEDULES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schedules.json")

# Valid states
State = Literal["occupied", "unoccupied"]
VALID_STATES = ["occupied", "unoccupied"]

# Days of the week (lowercase)
DAYS_OF_WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# =============================================================================
# SECTION 2: DEFAULT SCHEDULE
# =============================================================================
# Used when schedules.json doesn't exist or is corrupted.

def _create_default_schedules() -> dict:
    """
    Create a default schedule structure.
    
    Default pattern:
        - Weekdays: Occupied 06:00-08:00 and 16:00-22:00
        - Weekends: Occupied 08:00-23:00
    """
    
    # Helper to create a day's schedule
    def make_day(occupied_hours: list[int]) -> list[str]:
        return ["occupied" if h in occupied_hours else "unoccupied" for h in range(24)]
    
    weekday = make_day([6, 7, 16, 17, 18, 19, 20, 21])  # Morning + evening
    weekend = make_day([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22])  # Most of day
    
    return {
        "setpoints": {
            "occupied":   {"T_min": 20.0, "T_max": 23.0},
            "unoccupied": {"T_min": 17.0, "T_max": 30.0},
        },
        "rooms": {
            "default": {
                "weekly": {
                    "monday": weekday.copy(),
                    "tuesday": weekday.copy(),
                    "wednesday": weekday.copy(),
                    "thursday": weekday.copy(),
                    "friday": weekday.copy(),
                    "saturday": weekend.copy(),
                    "sunday": weekend.copy(),
                },
                "special_days": {
                    # Example: "2024-12-25": ["unoccupied"] * 24
                }
            }
        }
    }


# =============================================================================
# SECTION 3: FILE I/O
# =============================================================================

def _load_schedules() -> dict:
    """Load schedules from JSON file, or create default if missing."""
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, "r") as f:
                data = json.load(f)
                logger.debug("📅 Schedules loaded from file")
                # Migrate old flat setpoints to T_min/T_max format
                migrated = False
                for s, v in list(data.get("setpoints", {}).items()):
                    if isinstance(v, (int, float)):
                        data["setpoints"][s] = {"T_min": float(v), "T_max": float(v) + (5.0 if s == "occupied" else 6.0)}
                        logger.info(f"📅 Migrated setpoint {s!r} to T_min/T_max format")
                        migrated = True
                if migrated:
                    _save_schedules(data)
                return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"❌ Failed to load schedules: {e}")
            logger.info("📅 Using default schedules")
            return _create_default_schedules()
    else:
        logger.info("📅 No schedules file found, creating default")
        schedules = _create_default_schedules()
        _save_schedules(schedules)
        return schedules


def _save_schedules(schedules: dict) -> bool:
    """Save schedules to JSON file."""
    try:
        with open(SCHEDULES_FILE, "w") as f:
            json.dump(schedules, f, indent=2)
        logger.debug("📅 Schedules saved to file")
        return True
    except IOError as e:
        logger.error(f"❌ Failed to save schedules: {e}")
        return False


# In-memory cache (reloaded from file when needed)
_schedules_cache: dict | None = None


def _get_schedules() -> dict:
    """Get schedules (from cache or file)."""
    global _schedules_cache
    if _schedules_cache is None:
        _schedules_cache = _load_schedules()
    return _schedules_cache


def _update_schedules(schedules: dict) -> bool:
    """Update both cache and file."""
    global _schedules_cache
    _schedules_cache = schedules
    return _save_schedules(schedules)


def reload_schedules() -> dict:
    """Force reload from file (useful if file was edited externally)."""
    global _schedules_cache
    _schedules_cache = _load_schedules()
    return _schedules_cache


# =============================================================================
# SECTION 4: READ FUNCTIONS (for bot, MPC, logging)
# =============================================================================

def get_current_setpoint(room: str = "default") -> dict | None:
    """
    Get the current temperature setpoint.

    Args:
        room: Room name (default: "default")

    Returns:
        Dict with current setpoint info:
        {
            "T_min":   21.0,           # Lower comfort bound [°C]
            "T_max":   23.0,           # Upper comfort bound [°C]
            "setpoint": 21.0,          # Alias for T_min (backwards compat)
            "state":   "occupied",     # Current occupancy state
            "room":    "default",      # Room name
            "source":  "weekly",       # "weekly" or "special_day"
            "hour":    14,             # Current hour (0-23)
            "day":     "monday",       # Day of week
            "date":    "2024-02-17",   # Current date
        }

        Returns None if room not found.
    """
    schedules = _get_schedules()
    
    # Check if room exists
    if room not in schedules.get("rooms", {}):
        logger.warning(f"⚠️ Room '{room}' not found in schedules")
        return None
    
    room_schedule = schedules["rooms"][room]
    setpoints = schedules["setpoints"]
    
    now = datetime.now()
    hour = now.hour
    day_name = DAYS_OF_WEEK[now.weekday()]
    date_str = now.strftime("%Y-%m-%d")
    
    # Check special day first (priority)
    special_days = room_schedule.get("special_days", {})
    if date_str in special_days:
        state = special_days[date_str][hour]
        source = "special_day"
    else:
        # Use weekly schedule
        weekly = room_schedule.get("weekly", {})
        if day_name not in weekly:
            logger.error(f"❌ Day '{day_name}' not in weekly schedule")
            return None
        state = weekly[day_name][hour]
        source = "weekly"
    
    # Get T_min and T_max for this state
    sp    = _get_sp({"setpoints": setpoints}, state)
    T_min = sp["T_min"]
    T_max = sp["T_max"]

    return {
        "T_min":    T_min,
        "T_max":    T_max,
        "setpoint": T_min,
        "state":    state,
        "room":     room,
        "source":   source,
        "hour":     hour,
        "day":      day_name,
        "date":     date_str,
    }


def get_setpoint_horizon(steps: int, room: str = "default") -> dict | None:
    """
    Get setpoint forecast at 15-minute resolution for MPC use.

    Starts from the NEXT 15-min boundary (one step ahead of the current
    15-min boundary used by weather/prices), so that T_min[k] is the
    comfort constraint that must be met at the END of MPC step k.

    Example: MPC runs at 12:46 → weather/prices start at 12:45,
             schedule starts at 13:00, 13:15, 13:30, ...

    Args:
        steps: Number of 15-min steps required (e.g., 96 for 24h)
        room:  Room name (default: "default")

    Returns:
        Dict with structure:
        {
            "time":      [datetime, ...],
            "time_str":  ["2024-...", ...],
            "dt_minutes": 15,
            "n_steps":   steps,
            "T_min":     [21.0, 21.0, 17.0, ...],   # Lower comfort bound per step
            "T_max":     [23.0, 23.0, 22.0, ...],   # Upper comfort bound per step
            "setpoint":  [21.0, 21.0, 17.0, ...],   # Alias for T_min (backwards compat)
            "state":     ["occupied", "occupied", "unoccupied", ...],
            "room":      "default",
        }

        Returns None if room not found.
    """
    schedules = _get_schedules()

    if room not in schedules.get("rooms", {}):
        logger.warning(f"⚠️ Room '{room}' not found")
        return None

    room_schedule = schedules["rooms"][room]
    setpoints     = schedules["setpoints"]

    # Next 15-min boundary (one step ahead of current 15-min boundary)
    now            = datetime.now()
    minute_rounded = (now.minute // 15) * 15
    start_time     = now.replace(minute=minute_rounded, second=0, microsecond=0) + timedelta(minutes=15)

    result = {
        "time":      [],
        "time_str":  [],
        "dt_minutes": 15,
        "n_steps":   steps,
        "T_min":     [],
        "T_max":     [],
        "setpoint":  [],
        "state":     [],
        "room":      room,
    }

    for i in range(steps):
        t        = start_time + timedelta(minutes=15 * i)
        hour     = t.hour
        day_name = DAYS_OF_WEEK[t.weekday()]
        date_str = t.strftime("%Y-%m-%d")

        # Check special day first
        special_days = room_schedule.get("special_days", {})
        if date_str in special_days:
            state = special_days[date_str][hour]
        else:
            state = room_schedule["weekly"][day_name][hour]

        sp    = _get_sp({"setpoints": setpoints}, state)
        T_min = sp["T_min"]
        T_max = sp["T_max"]

        result["time"].append(t)
        result["time_str"].append(t.strftime("%Y-%m-%dT%H:%M"))
        result["T_min"].append(T_min)
        result["T_max"].append(T_max)
        result["setpoint"].append(T_min)
        result["state"].append(state)

    logger.info(f"📅 Setpoint horizon: {steps} steps (15-min) from {start_time.strftime('%H:%M')} for room '{room}'")

    return result


def get_rooms() -> list[str]:
    """Get list of all room names."""
    schedules = _get_schedules()
    return list(schedules.get("rooms", {}).keys())


def get_setpoint_temperatures() -> dict:
    """Get the temperature values for each state."""
    schedules = _get_schedules()
    return schedules.get("setpoints", {}).copy()


def get_weekly_schedule(room: str = "default") -> dict | None:
    """Get the weekly schedule for a room."""
    schedules = _get_schedules()
    if room not in schedules.get("rooms", {}):
        return None
    return schedules["rooms"][room].get("weekly", {}).copy()


def get_special_days(room: str = "default") -> dict | None:
    """Get all special days for a room."""
    schedules = _get_schedules()
    if room not in schedules.get("rooms", {}):
        return None
    return schedules["rooms"][room].get("special_days", {}).copy()


# =============================================================================
# SECTION 5: WRITE FUNCTIONS (for chatbot tools)
# =============================================================================

def _get_sp(schedules: dict, state: str) -> dict:
    """Return the setpoint dict for a state, migrating flat values if needed."""
    sp = schedules["setpoints"].get(state, {})
    if not isinstance(sp, dict):
        sp = {"T_min": float(sp), "T_max": float(sp) + 5.0}
    return sp


def _validate_setpoint(sp: dict, state: str) -> str | None:
    """
    Validate a setpoint dict. Returns an error string if invalid, else None.

    Rules:
        - T_max must be strictly greater than T_min
        - Both must be within the physical range 5–35°C
    """
    T_min = sp.get("T_min")
    T_max = sp.get("T_max")
    if T_min is None or T_max is None:
        return f"Setpoint for '{state}' is missing T_min or T_max"
    if T_max <= T_min:
        return f"T_max ({T_max}°C) must be greater than T_min ({T_min}°C) for state '{state}'"
    if not (5.0 <= T_min <= 35.0):
        return f"T_min ({T_min}°C) is outside the allowed range 5–35°C for state '{state}'"
    if not (5.0 <= T_max <= 35.0):
        return f"T_max ({T_max}°C) is outside the allowed range 5–35°C for state '{state}'"
    return None


def set_setpoint_temperature(state: State, temperature: float) -> bool:
    """
    Set T_min (lower comfort bound) for a state.

    Args:
        state:       "occupied" or "unoccupied"
        temperature: New T_min value in °C

    Returns:
        True if successful, False if validation fails or state is invalid.

    Example (chatbot):
        "Set the occupied minimum temperature to 21 degrees"
        → set_setpoint_temperature("occupied", 21.0)
    """
    if state not in VALID_STATES:
        logger.error(f"❌ Invalid state: {state}")
        return False
    schedules = _get_schedules()
    sp = _get_sp(schedules, state)
    sp["T_min"] = float(temperature)
    err = _validate_setpoint(sp, state)
    if err:
        logger.error(f"❌ {err}")
        return False
    schedules["setpoints"][state] = sp
    logger.info(f"📅 Set {state} T_min to {temperature}°C")
    return _update_schedules(schedules)


def set_setpoint_t_max(state: State, temperature: float) -> bool:
    """
    Set T_max (upper comfort bound) for a state.

    Args:
        state:       "occupied" or "unoccupied"
        temperature: New T_max value in °C

    Returns:
        True if successful, False if validation fails or state is invalid.

    Example (chatbot):
        "Set the occupied maximum temperature to 23 degrees"
        → set_setpoint_t_max("occupied", 23.0)
    """
    if state not in VALID_STATES:
        logger.error(f"❌ Invalid state: {state}")
        return False
    schedules = _get_schedules()
    sp = _get_sp(schedules, state)
    sp["T_max"] = float(temperature)
    err = _validate_setpoint(sp, state)
    if err:
        logger.error(f"❌ {err}")
        return False
    schedules["setpoints"][state] = sp
    logger.info(f"📅 Set {state} T_max to {temperature}°C")
    return _update_schedules(schedules)


def set_weekly_hour(day: str, hour: int, state: State, room: str = "default") -> bool:
    """
    Set a single hour in the weekly schedule.
    
    Args:
        day: Day of week (e.g., "monday")
        hour: Hour (0-23)
        state: "occupied" or "unoccupied"
        room: Room name (default: "default")
    
    Returns:
        True if successful
    
    Example (chatbot):
        "Set Monday 9am to occupied"
        → set_weekly_hour("monday", 9, "occupied")
    """
    day = day.lower()
    if day not in DAYS_OF_WEEK:
        logger.error(f"❌ Invalid day: {day}")
        return False
    if not 0 <= hour <= 23:
        logger.error(f"❌ Invalid hour: {hour}")
        return False
    if state not in VALID_STATES:
        logger.error(f"❌ Invalid state: {state}")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    schedules["rooms"][room]["weekly"][day][hour] = state
    
    logger.info(f"📅 Set {room}/{day} hour {hour} to {state}")
    return _update_schedules(schedules)


def set_weekly_range(day: str, start_hour: int, end_hour: int, state: State, room: str = "default") -> bool:
    """
    Set a range of hours in the weekly schedule.
    
    Args:
        day: Day of week (e.g., "monday")
        start_hour: Start hour (0-23), inclusive
        end_hour: End hour (0-23), inclusive
        state: "occupied" or "unoccupied"
        room: Room name
    
    Returns:
        True if successful
    
    Example (chatbot):
        "Set Monday 9am to 5pm as occupied"
        → set_weekly_range("monday", 9, 17, "occupied")
    """
    day = day.lower()
    if day not in DAYS_OF_WEEK:
        logger.error(f"❌ Invalid day: {day}")
        return False
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        logger.error(f"❌ Invalid hour range: {start_hour}-{end_hour}")
        return False
    if state not in VALID_STATES:
        logger.error(f"❌ Invalid state: {state}")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    for h in range(start_hour, end_hour + 1):
        schedules["rooms"][room]["weekly"][day][h] = state
    
    logger.info(f"📅 Set {room}/{day} hours {start_hour}-{end_hour} to {state}")
    return _update_schedules(schedules)


def set_weekly_day(day: str, states: list[State], room: str = "default") -> bool:
    """
    Set the entire schedule for a day.
    
    Args:
        day: Day of week (e.g., "monday")
        states: List of 24 states, one per hour
        room: Room name
    
    Returns:
        True if successful
    
    Example (chatbot):
        "Make Monday fully occupied"
        → set_weekly_day("monday", ["occupied"] * 24)
    """
    day = day.lower()
    if day not in DAYS_OF_WEEK:
        logger.error(f"❌ Invalid day: {day}")
        return False
    if len(states) != 24:
        logger.error(f"❌ States must have 24 entries, got {len(states)}")
        return False
    if not all(s in VALID_STATES for s in states):
        logger.error(f"❌ Invalid state in list")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    schedules["rooms"][room]["weekly"][day] = states.copy()
    
    logger.info(f"📅 Set {room}/{day} full schedule")
    return _update_schedules(schedules)


def copy_weekly_day(from_day: str, to_day: str, room: str = "default") -> bool:
    """
    Copy one day's schedule to another day.
    
    Example (chatbot):
        "Make Tuesday the same as Monday"
        → copy_weekly_day("monday", "tuesday")
    """
    from_day = from_day.lower()
    to_day = to_day.lower()
    
    if from_day not in DAYS_OF_WEEK or to_day not in DAYS_OF_WEEK:
        logger.error(f"❌ Invalid day")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    schedules["rooms"][room]["weekly"][to_day] = schedules["rooms"][room]["weekly"][from_day].copy()
    
    logger.info(f"📅 Copied {room}/{from_day} to {to_day}")
    return _update_schedules(schedules)


def set_special_day(date: str, states: list[State], room: str = "default") -> bool:
    """
    Set a special day schedule (overrides weekly).
    
    Args:
        date: Date string "YYYY-MM-DD"
        states: List of 24 states, one per hour
        room: Room name
    
    Returns:
        True if successful
    
    Example (chatbot):
        "I'm away on December 25th"
        → set_special_day("2024-12-25", ["unoccupied"] * 24)
    """
    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        logger.error(f"❌ Invalid date format: {date}")
        return False
    
    if len(states) != 24:
        logger.error(f"❌ States must have 24 entries")
        return False
    if not all(s in VALID_STATES for s in states):
        logger.error(f"❌ Invalid state in list")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    schedules["rooms"][room]["special_days"][date] = states.copy()
    
    logger.info(f"📅 Set special day {date} for {room}")
    return _update_schedules(schedules)


def set_special_day_away(date: str, room: str = "default") -> bool:
    """
    Quick helper: Set a day as fully unoccupied (away).
    
    Example (chatbot):
        "I'll be away on January 15th"
        → set_special_day_away("2024-01-15")
    """
    return set_special_day(date, ["unoccupied"] * 24, room)


def set_special_day_home(date: str, room: str = "default") -> bool:
    """
    Quick helper: Set a day as fully occupied (home all day).
    
    Example (chatbot):
        "I'll be home all day on Saturday the 20th"
        → set_special_day_home("2024-01-20")
    """
    return set_special_day(date, ["occupied"] * 24, room)


def remove_special_day(date: str, room: str = "default") -> bool:
    """
    Remove a special day (revert to weekly schedule).
    
    Example (chatbot):
        "Cancel the special schedule for December 25th"
        → remove_special_day("2024-12-25")
    """
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.error(f"❌ Room '{room}' not found")
        return False
    
    special_days = schedules["rooms"][room].get("special_days", {})
    
    if date in special_days:
        del special_days[date]
        logger.info(f"📅 Removed special day {date} for {room}")
        return _update_schedules(schedules)
    else:
        logger.warning(f"⚠️ Special day {date} not found for {room}")
        return False


def clear_past_special_days(room: str = "default") -> int:
    """
    Remove all special days in the past.
    
    Returns:
        Number of days removed
    """
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        return 0
    
    today = datetime.now().strftime("%Y-%m-%d")
    special_days = schedules["rooms"][room].get("special_days", {})
    
    past_dates = [d for d in special_days if d < today]
    
    for date in past_dates:
        del special_days[date]
    
    if past_dates:
        _update_schedules(schedules)
        logger.info(f"📅 Cleared {len(past_dates)} past special days for {room}")
    
    return len(past_dates)


# =============================================================================
# SECTION 6: ROOM MANAGEMENT (for future multi-room support)
# =============================================================================

def add_room(room: str, copy_from: str = "default") -> bool:
    """
    Add a new room, optionally copying schedule from another room.
    
    Example (chatbot):
        "Add a bedroom schedule"
        → add_room("bedroom")
    """
    schedules = _get_schedules()
    
    if room in schedules.get("rooms", {}):
        logger.warning(f"⚠️ Room '{room}' already exists")
        return False
    
    if copy_from not in schedules.get("rooms", {}):
        logger.error(f"❌ Source room '{copy_from}' not found")
        return False
    
    # Deep copy the source room
    import copy
    schedules["rooms"][room] = copy.deepcopy(schedules["rooms"][copy_from])
    
    logger.info(f"📅 Added room '{room}' (copied from '{copy_from}')")
    return _update_schedules(schedules)


def remove_room(room: str) -> bool:
    """
    Remove a room.
    
    Note: Cannot remove "default" room.
    """
    if room == "default":
        logger.error("❌ Cannot remove default room")
        return False
    
    schedules = _get_schedules()
    
    if room not in schedules.get("rooms", {}):
        logger.warning(f"⚠️ Room '{room}' not found")
        return False
    
    del schedules["rooms"][room]
    
    logger.info(f"📅 Removed room '{room}'")
    return _update_schedules(schedules)


# =============================================================================
# SECTION 7: STANDALONE TEST MODE
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  SCHEDULE MANAGER TEST")
    print("=" * 60)
    
    # Current setpoint
    print("\n📍 CURRENT SETPOINT")
    print("-" * 40)
    current = get_current_setpoint()
    if current:
        print(f"   Room:     {current['room']}")
        print(f"   Setpoint: {current['setpoint']}°C ({current['state']})")
        print(f"   Source:   {current['source']}")
        print(f"   Time:     {current['day']} {current['hour']}:00")
    
    # Setpoint temperatures
    print("\n🌡️ SETPOINT TEMPERATURES")
    print("-" * 40)
    temps = get_setpoint_temperatures()
    for state, temp in temps.items():
        print(f"   {state}: {temp}°C")
    
    # Weekly schedule summary
    print("\n📅 WEEKLY SCHEDULE (default room)")
    print("-" * 40)
    weekly = get_weekly_schedule()
    if weekly:
        for day in DAYS_OF_WEEK:
            states = weekly[day]
            occupied_hours = [h for h, s in enumerate(states) if s == "occupied"]
            if occupied_hours:
                ranges = []
                start = occupied_hours[0]
                end = start
                for h in occupied_hours[1:]:
                    if h == end + 1:
                        end = h
                    else:
                        ranges.append(f"{start:02d}-{end+1:02d}")
                        start = end = h
                ranges.append(f"{start:02d}-{end+1:02d}")
                print(f"   {day.capitalize():12} Occupied: {', '.join(ranges)}")
            else:
                print(f"   {day.capitalize():12} Unoccupied all day")
    
    # Test adding a special day (away all day)
    print("\n🧪 TEST: Adding special day (away all day)")
    print("-" * 40)
    
    # Use tomorrow's date for the test
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"   Adding special day: {tomorrow} (away all day)")
    
    success = set_special_day_away(tomorrow)
    if success:
        print(f"   ✅ Successfully added special day")
    else:
        print(f"   ❌ Failed to add special day")
    
    # Special days
    print("\n🎄 SPECIAL DAYS")
    print("-" * 40)
    special = get_special_days()
    if special:
        for date, states in sorted(special.items()):
            occ_count = sum(1 for s in states if s == "occupied")
            print(f"   {date}: {occ_count}/24 hours occupied")
    else:
        print("   (none)")
    
    # Test horizon (should show the special day if it's within range)
    print("\n🎯 SETPOINT HORIZON (next 36 hours)")
    print("-" * 40)
    horizon = get_setpoint_horizon(hours=36)
    if horizon:
        print(f"   {'Time':<18} {'Setpoint':>10} {'State':<12}")
        for i in range(horizon["n_steps"]):
            t = horizon["time_str"][i]
            sp = horizon["setpoint"][i]
            state = horizon["state"][i]
            # Highlight if this falls on the special day
            marker = " ← special day" if t.startswith(tomorrow) else ""
            print(f"   {t:<18} {sp:>10.1f} {state:<12}{marker}")
    
    # Rooms
    print("\n🏠 ROOMS")
    print("-" * 40)
    rooms = get_rooms()
    print(f"   {', '.join(rooms)}")
    
    print()