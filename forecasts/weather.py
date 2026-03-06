# =============================================================================
# WEATHER - Open-Meteo API Integration
# =============================================================================
# Fetches weather data from Open-Meteo (free, no API key required).
#
# Two main functions:
#   - fetch_current_weather() → Current conditions (for logging to InfluxDB)
#   - fetch_forecast()        → 7-day forecast at 15-min resolution (for MPC)
#
# API docs: https://open-meteo.com/en/docs
# =============================================================================

import json
import logging
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: API CONFIGURATION
# =============================================================================

API_BASE = "https://api.open-meteo.com/v1/forecast"

# Fields for current weather
CURRENT_FIELDS = [
    # Basic
    "temperature_2m",
    "relative_humidity_2m",
    "rain",
    "weather_code",
    "pressure_msl",
    "is_day",
    # Wind
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    # Solar (horizontal)
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    # Soil
    "soil_temperature_0cm",
    "soil_temperature_6cm",
    "soil_temperature_18cm",
    "soil_temperature_54cm",
]

# Fields for 15-minute forecast
MINUTELY_15_FIELDS = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "global_tilted_irradiance",
]

# Fields only available hourly
HOURLY_FIELDS = [
    "pressure_msl",
    "cloud_cover",
    "soil_temperature_0cm",
    "soil_temperature_6cm",
    "soil_temperature_18cm",
    "soil_temperature_54cm",
]


# =============================================================================
# SECTION 2: API HELPERS
# =============================================================================

def _fetch_url(url: str, timeout: int = 15) -> dict | None:
    """Fetch JSON from a URL. Returns None on error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        logger.error(f"❌ Weather API error: {e}")
        return None


# =============================================================================
# SECTION 3: CURRENT WEATHER (for logging to InfluxDB)
# =============================================================================

def fetch_current_weather() -> dict | None:
    """
    Fetch current weather conditions.
    
    Makes one API call for base fields, plus one per solar surface.
    
    Returns:
        Dict with standardized field names:
        {
            # Basic
            "temp": 5.2, "hum": 80, "rain": 0.0, "pressure": 1013.2,
            "code": 3, "is_day": 1,
            
            # Wind
            "wind_speed": 12.5, "wind_dir": 225, "wind_gust": 25.0,
            
            # Solar - horizontal (W/m²)
            "solar_ghi": 450, "solar_direct": 300, 
            "solar_diffuse": 150, "solar_dni": 600,
            
            # Solar - per surface (W/m²)
            "solar_south": 400, "solar_west": 150, ...
            
            # Soil (°C)
            "soil_0cm": 8.5, "soil_6cm": 9.2, 
            "soil_18cm": 10.1, "soil_54cm": 11.5,
        }
        
        Returns None if request fails.
    """
    # Build URL for base fields
    fields = ",".join(CURRENT_FIELDS)
    url = (
        f"{API_BASE}"
        f"?latitude={config.LATITUDE}"
        f"&longitude={config.LONGITUDE}"
        f"&current={fields}"
    )
    
    data = _fetch_url(url)
    if not data:
        return None
    
    c = data.get("current", {})
    
    # Map to standardized names
    result = {
        # Basic
        "temp": c.get("temperature_2m"),
        "hum": c.get("relative_humidity_2m"),
        "rain": c.get("rain"),
        "pressure": c.get("pressure_msl"),
        "code": c.get("weather_code"),
        "is_day": c.get("is_day"),
        # Wind
        "wind_speed": c.get("wind_speed_10m"),
        "wind_dir": c.get("wind_direction_10m"),
        "wind_gust": c.get("wind_gusts_10m"),
        # Solar - horizontal
        "solar_ghi": c.get("shortwave_radiation"),
        "solar_direct": c.get("direct_radiation"),
        "solar_diffuse": c.get("diffuse_radiation"),
        "solar_dni": c.get("direct_normal_irradiance"),
        # Soil
        "soil_0cm": c.get("soil_temperature_0cm"),
        "soil_6cm": c.get("soil_temperature_6cm"),
        "soil_18cm": c.get("soil_temperature_18cm"),
        "soil_54cm": c.get("soil_temperature_54cm"),
    }
    
    # Fetch GTI for each solar surface
    for surface in config.SOLAR_SURFACES:
        name = surface["name"]
        tilt = surface["tilt"]
        azimuth = surface["azimuth"]
        
        gti_url = (
            f"{API_BASE}"
            f"?latitude={config.LATITUDE}"
            f"&longitude={config.LONGITUDE}"
            f"&current=global_tilted_irradiance"
            f"&tilt={tilt}"
            f"&azimuth={azimuth}"
        )
        
        gti_data = _fetch_url(gti_url, timeout=10)
        
        if gti_data:
            result[f"solar_{name}"] = gti_data.get("current", {}).get("global_tilted_irradiance")
        else:
            result[f"solar_{name}"] = None
            logger.warning(f"⚠️ Failed to fetch GTI for {name}")
    
    logger.info(f"🌤️ Weather: {result['temp']}°C, GHI={result['solar_ghi']} W/m²")
    return result


# =============================================================================
# SECTION 4: FORECAST (for MPC)
# =============================================================================

def fetch_forecast(days: int = 7) -> dict | None:
    """
    Fetch multi-day weather forecast at 15-minute resolution.
    
    Args:
        days: Number of forecast days (1-16, default 7)
    
    Returns:
        Dict with structure:
        {
            "minutely_15": {
                "time": ["2024-01-15T00:00", ...],
                "temp": [5.2, 5.1, ...],
                "solar_ghi": [0, 45, ...],
                "solar_south": [0, 60, ...],
                ...
            },
            "hourly": {
                "time": ["2024-01-15T00:00", ...],
                "pressure": [1013.2, ...],
                ...
            },
            "surfaces": ["south", "west", "east", "north"]
        }
        
        Returns None if request fails.
    """
    result = {
        "minutely_15": {},
        "hourly": {},
        "surfaces": []
    }
    
    # Get first surface for initial request
    first_surface = config.SOLAR_SURFACES[0] if config.SOLAR_SURFACES else None
    tilt = first_surface["tilt"] if first_surface else 90
    azimuth = first_surface["azimuth"] if first_surface else 0
    
    # Build URL
    minutely_str = ",".join(MINUTELY_15_FIELDS)
    hourly_str = ",".join(HOURLY_FIELDS)
    
    url = (
        f"{API_BASE}"
        f"?latitude={config.LATITUDE}"
        f"&longitude={config.LONGITUDE}"
        f"&minutely_15={minutely_str}"
        f"&hourly={hourly_str}"
        f"&forecast_days={days}"
        f"&tilt={tilt}"
        f"&azimuth={azimuth}"
        f"&timezone=auto"
    )
    
    data = _fetch_url(url, timeout=30)
    if not data:
        return None
    
    # Process minutely_15 data
    m15 = data.get("minutely_15", {})
    result["minutely_15"] = {
        "time": m15.get("time", []),
        "temp": m15.get("temperature_2m", []),
        "hum": m15.get("relative_humidity_2m", []),
        "precipitation": m15.get("precipitation", []),
        "code": m15.get("weather_code", []),
        "wind_speed": m15.get("wind_speed_10m", []),
        "wind_dir": m15.get("wind_direction_10m", []),
        "wind_gust": m15.get("wind_gusts_10m", []),
        "solar_ghi": m15.get("shortwave_radiation", []),
        "solar_direct": m15.get("direct_radiation", []),
        "solar_diffuse": m15.get("diffuse_radiation", []),
        "solar_dni": m15.get("direct_normal_irradiance", []),
    }
    
    # Add first surface GTI
    if first_surface:
        result["minutely_15"][f"solar_{first_surface['name']}"] = m15.get("global_tilted_irradiance", [])
        result["surfaces"].append(first_surface["name"])
    
    # Process hourly data
    hourly = data.get("hourly", {})
    result["hourly"] = {
        "time": hourly.get("time", []),
        "pressure": hourly.get("pressure_msl", []),
        "cloud_cover": hourly.get("cloud_cover", []),
        "soil_0cm": hourly.get("soil_temperature_0cm", []),
        "soil_6cm": hourly.get("soil_temperature_6cm", []),
        "soil_18cm": hourly.get("soil_temperature_18cm", []),
        "soil_54cm": hourly.get("soil_temperature_54cm", []),
    }
    
    # Fetch additional solar surfaces
    for surface in config.SOLAR_SURFACES[1:]:
        name = surface["name"]
        
        surface_url = (
            f"{API_BASE}"
            f"?latitude={config.LATITUDE}"
            f"&longitude={config.LONGITUDE}"
            f"&minutely_15=global_tilted_irradiance"
            f"&forecast_days={days}"
            f"&tilt={surface['tilt']}"
            f"&azimuth={surface['azimuth']}"
            f"&timezone=auto"
        )
        
        surface_data = _fetch_url(surface_url, timeout=30)
        
        if surface_data:
            gti = surface_data.get("minutely_15", {}).get("global_tilted_irradiance", [])
            result["minutely_15"][f"solar_{name}"] = gti
            result["surfaces"].append(name)
        else:
            logger.warning(f"⚠️ Failed to fetch forecast GTI for {name}")
    
    n_15 = len(result["minutely_15"].get("time", []))
    n_h = len(result["hourly"].get("time", []))
    logger.info(f"📅 Forecast: {n_15} x 15min, {n_h} x hourly, {len(result['surfaces'])} surfaces")
    
    return result


# =============================================================================
# SECTION 5: MPC HELPER FUNCTIONS
# =============================================================================

def get_weather_horizon(forecast: dict, hours: int) -> dict | None:
    """
    Extract forecast from now to now + N hours.
    
    For MPC: returns aligned time series at 15-minute resolution.
    
    Args:
        forecast: Result from fetch_forecast()
        hours: Prediction horizon in hours (e.g., 24)
    
    Returns:
        Dict with structure:
        {
            "time": [datetime, ...],
            "time_str": ["2024-...", ...],
            "dt_minutes": 15,
            "n_steps": 96,
            "temp": [5.2, ...],
            "solar_ghi": [450, ...],
            ...
        }
        
        Returns None if forecast doesn't cover the horizon.
    """
    m15 = forecast.get("minutely_15", {})
    times_str = m15.get("time", [])
    
    if not times_str:
        return None
    
    # Parse times
    forecast_times = [datetime.fromisoformat(t) for t in times_str]
    
    # Find start: round down to nearest 15 minutes
    now = datetime.now()
    minute_rounded = (now.minute // 15) * 15
    start_time = now.replace(minute=minute_rounded, second=0, microsecond=0)
    
    # Find start index
    start_idx = None
    for i, t in enumerate(forecast_times):
        if t >= start_time:
            start_idx = i
            break
    
    if start_idx is None:
        logger.error("Forecast doesn't cover current time")
        return None
    
    # Number of steps (4 per hour)
    n_steps = hours * 4
    end_idx = start_idx + n_steps
    
    if end_idx > len(forecast_times):
        available = len(forecast_times) - start_idx
        logger.warning(f"Forecast only covers {available} steps, requested {n_steps}")
        end_idx = len(forecast_times)
        n_steps = end_idx - start_idx
    
    # Build result
    result = {
        "time": forecast_times[start_idx:end_idx],
        "time_str": times_str[start_idx:end_idx],
        "dt_minutes": 15,
        "n_steps": n_steps,
    }
    
    # Copy all data arrays
    for key, values in m15.items():
        if key != "time" and isinstance(values, list):
            result[key] = values[start_idx:end_idx]
    
    logger.info(f"🎯 Weather horizon: {n_steps} steps ({hours}h) from {start_time.strftime('%H:%M')}")
    
    return result


def weather_to_numpy(horizon: dict) -> dict:
    """Convert weather horizon to numpy arrays."""
    try:
        import numpy as np
        
        result = {
            "time": horizon["time"],
            "time_str": horizon["time_str"],
            "dt_minutes": horizon["dt_minutes"],
            "n_steps": horizon["n_steps"],
        }
        
        for key, values in horizon.items():
            if isinstance(values, list) and key not in ["time", "time_str"]:
                result[key] = np.array([v if v is not None else np.nan for v in values])
        
        return result
        
    except ImportError:
        logger.warning("NumPy not available")
        return horizon


# =============================================================================
# SECTION 6: HELPER FUNCTIONS
# =============================================================================

WMO_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Slight showers",
    81: "Moderate showers",
    82: "Violent showers",
    95: "Thunderstorm",
}

WIND_DIRECTIONS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
]


def get_weather_description(code: int) -> str:
    """Convert WMO weather code to description."""
    return WMO_DESCRIPTIONS.get(code, "Unknown")


def get_wind_direction_name(degrees: float) -> str:
    """Convert degrees to compass direction (N, NE, etc.)."""
    if degrees is None:
        return "N/A"
    index = round(degrees / 22.5) % 16
    return WIND_DIRECTIONS[index]


def summarize_day(forecast: dict, date: datetime) -> dict:
    """Summarize forecast for a specific day."""
    m15 = forecast.get("minutely_15", {})
    times = m15.get("time", [])
    temps = m15.get("temp", [])
    
    date_str = date.strftime("%Y-%m-%d")
    
    day_temps = []
    day_solar = []
    
    for i, t in enumerate(times):
        if t.startswith(date_str):
            if i < len(temps) and temps[i] is not None:
                day_temps.append(temps[i])
            ghi = m15.get("solar_ghi", [])
            if i < len(ghi) and ghi[i] is not None:
                day_solar.append(ghi[i])
    
    return {
        "date": date_str,
        "temp_min": min(day_temps) if day_temps else None,
        "temp_max": max(day_temps) if day_temps else None,
        "temp_avg": round(sum(day_temps) / len(day_temps), 1) if day_temps else None,
        "solar_total_kwh": round(sum(day_solar) * 0.25 / 1000, 2) if day_solar else None,
    }


# =============================================================================
# SECTION 7: STANDALONE TEST MODE
# =============================================================================

if __name__ == "__main__":
    
    # --- Test current weather ---
    print("=" * 60)
    print("  CURRENT WEATHER")
    print("=" * 60)
    
    current = fetch_current_weather()
    
    if current:
        code = current.get('code')
        is_day = "☀️ Day" if current.get('is_day') else "🌙 Night"
        wind_dir = get_wind_direction_name(current.get('wind_dir'))
        
        print(f"\n{get_weather_description(code)} ({is_day})")
        print(f"\n📊 Basic:")
        print(f"   Temp: {current['temp']}°C | Humidity: {current['hum']}%")
        print(f"   Pressure: {current['pressure']} hPa | Rain: {current['rain']} mm")
        print(f"\n💨 Wind:")
        print(f"   {current['wind_speed']} km/h from {wind_dir} (gusts {current['wind_gust']})")
        print(f"\n☀️ Solar (W/m²):")
        print(f"   GHI: {current['solar_ghi']} | Direct: {current['solar_direct']} | Diffuse: {current['solar_diffuse']}")
        print(f"   Walls: ", end="")
        for s in config.SOLAR_SURFACES:
            val = current.get(f"solar_{s['name']}", "N/A")
            val_s = f"{val:.0f}" if val is not None else "N/A"
            print(f"{s['name']}={val_s} ", end="")
        print()
    else:
        print("❌ Failed to fetch current weather")
    
    # --- Test forecast ---
    print("\n" + "=" * 60)
    print("  7-DAY FORECAST")
    print("=" * 60)
    
    forecast = fetch_forecast(days=7)
    
    if forecast:
        n_15 = len(forecast["minutely_15"].get("time", []))
        n_h = len(forecast["hourly"].get("time", []))
        
        print(f"\n📊 Data: {n_15} × 15-min, {n_h} × hourly")
        print(f"🧱 Surfaces: {', '.join(forecast['surfaces'])}")
        
        print("\n" + "-" * 60)
        print(f"{'Date':<12} {'Min':>6} {'Max':>6} {'Avg':>6} {'Solar':>10}")
        print(f"{'':12} {'°C':>6} {'°C':>6} {'°C':>6} {'kWh/m²':>10}")
        print("-" * 60)
        
        today = datetime.now().date()
        for i in range(7):
            day = datetime.combine(today + timedelta(days=i), datetime.min.time())
            s = summarize_day(forecast, day)
            if s["temp_min"] is not None:
                print(f"{s['date']:<12} {s['temp_min']:>6.1f} {s['temp_max']:>6.1f} "
                      f"{s['temp_avg']:>6.1f} {s['solar_total_kwh']:>10.2f}")
        
        # Test MPC horizon
        print("\n" + "-" * 60)
        print("  MPC HORIZON (24h)")
        print("-" * 60)
        
        horizon = get_weather_horizon(forecast, hours=24)
        if horizon:
            print(f"\n✅ {horizon['n_steps']} steps at {horizon['dt_minutes']}-min resolution")
            print(f"   From: {horizon['time_str'][0]}")
            print(f"   To:   {horizon['time_str'][-1]}")
        else:
            print("❌ Failed to extract horizon")
    else:
        print("❌ Failed to fetch forecast")
    
    print()