# =============================================================================
# PRICES - Danish Electricity Prices
# =============================================================================
# Fetches day-ahead spot prices from Energi Data Service.
# Prices are at 15-minute resolution and typically available after 13:00.
#
# API docs: https://www.energidataservice.dk/tso-electricity/DayAheadPrices
#
# Sections:
#   1. Configuration
#   2. API Helpers
#   3. Current Price (for state store / InfluxDB)
#   4. Forecast Prices (for MPC)
#   5. Tariff Calculations
#   6. MPC Helper Functions
# =============================================================================

import json
import logging
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

API_BASE = "https://api.energidataservice.dk/dataset/DayAheadPrices"

# Price area: DK1 = Western Denmark (Jutland), DK2 = Eastern Denmark (Zealand)
PRICE_AREA = getattr(config, 'PRICE_AREA', 'DK1')


# =============================================================================
# SECTION 2: API HELPERS
# =============================================================================

def _fetch_url(url: str) -> dict | None:
    """Fetch JSON from a URL. Returns None on error."""
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode())
    except HTTPError as e:
        logger.error(f"❌ HTTP Error {e.code}: {e.reason}")
        return None
    except (URLError, json.JSONDecodeError) as e:
        logger.error(f"❌ Price API error: {e}")
        return None


def _build_price_url(start: datetime, end: datetime, limit: int = 5000) -> str:
    """Build API URL for a price query."""
    params = {
        "start":  start.strftime("%Y-%m-%dT%H:%M"),
        "end":    end.strftime("%Y-%m-%dT%H:%M"),
        "filter": json.dumps({"PriceArea": PRICE_AREA}),
        "sort":   "TimeDK asc",
        "limit":  limit,
    }
    return f"{API_BASE}?{urllib.parse.urlencode(params)}"


# =============================================================================
# SECTION 3: CURRENT PRICE (for state store / InfluxDB)
# =============================================================================

def fetch_current_price() -> dict | None:
    """
    Fetch the current electricity price.

    Returns a flat dict suitable for the state store and InfluxDB logging:
        {
            "price_spot":       0.85,   # DKK/kWh (spot only)
            "price_full":       2.45,   # DKK/kWh (incl. tariffs + VAT)
            "price_full_ex_vat": 1.96,  # DKK/kWh (incl. tariffs, no VAT)
            "grid_tariff":      0.45,   # DKK/kWh (current grid tariff)
            "price_eur":        114.2,  # EUR/MWh
            "area":             "DK1",
        }

    Returns None if request fails.
    """
    now = datetime.now()
    url = _build_price_url(now - timedelta(hours=1), now + timedelta(hours=1), limit=20)
    data = _fetch_url(url)

    if not data:
        return None

    records = data.get("records", [])
    if not records:
        return None

    # Find the most recent record that is not in the future
    current_record = None
    for record in sorted(records, key=lambda r: r.get("TimeDK", ""), reverse=True):
        try:
            if datetime.fromisoformat(record.get("TimeDK", "")) <= now:
                current_record = record
                break
        except ValueError:
            continue

    if not current_record:
        current_record = records[-1]

    price_dkk_mwh = current_record.get("DayAheadPriceDKK")
    if price_dkk_mwh is None:
        return None

    price_spot = round(price_dkk_mwh / 1000, 4)

    result = {
        "price_spot":        price_spot,
        "price_full":        calculate_full_price(price_spot, now, include_vat=True),
        "price_full_ex_vat": calculate_full_price(price_spot, now, include_vat=False),
        "grid_tariff":       get_grid_tariff(now),
        "price_eur":         current_record.get("DayAheadPriceEUR"),
        "area":              PRICE_AREA,
    }

    logger.info(f"💰 Current price: {price_spot:.2f} DKK/kWh (full: {result['price_full']:.2f})")
    return result


# =============================================================================
# SECTION 4: FORECAST PRICES (for MPC)
# =============================================================================

def fetch_prices(days_back: int = 0, days_forward: int = 2) -> dict | None:
    """
    Fetch electricity prices for a time range at 15-minute resolution.

    Day-ahead prices for tomorrow are typically available after 13:00 CET.

    Args:
        days_back:    Days of historical prices to include (default 0)
        days_forward: Days of future prices to include (default 2)

    Returns:
        Dict with aligned price arrays:
            time, time_utc, price_dkk, price_eur, price_dkk_kwh
        or None on failure.
    """
    now = datetime.now()
    start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0)
    end   = (now + timedelta(days=days_forward)).replace(hour=23, minute=59, second=0)

    data = _fetch_url(_build_price_url(start, end))
    if not data:
        return None

    records = sorted(data.get("records", []), key=lambda r: r.get("TimeDK", ""))
    if not records:
        logger.warning("No price records returned")
        return None

    result = {
        "time":         [],
        "time_utc":     [],
        "price_dkk":    [],
        "price_eur":    [],
        "price_dkk_kwh": [],
        "area":         PRICE_AREA,
        "n_records":    len(records),
    }

    for record in records:
        price_dkk = record.get("DayAheadPriceDKK")
        result["time"].append(record.get("TimeDK", ""))
        result["time_utc"].append(record.get("TimeUTC", ""))
        result["price_dkk"].append(price_dkk)
        result["price_eur"].append(record.get("DayAheadPriceEUR"))
        result["price_dkk_kwh"].append(
            round(price_dkk / 1000, 4) if price_dkk is not None else None
        )

    logger.info(f"💰 Prices fetched: {len(records)} records for {PRICE_AREA}")
    return result


# =============================================================================
# SECTION 5: TARIFF CALCULATIONS
# =============================================================================

def get_current_period(dt: datetime = None) -> str:
    """
    Return the time-of-use period for the given datetime.

    Uses hour definitions from config.ELECTRICITY_TARIFFS so there is
    no hardcoding of hours anywhere else in the codebase.

    Args:
        dt: Datetime to check (default: now)

    Returns:
        "low", "medium", or "high"
    """
    if dt is None:
        dt = datetime.now()

    hours_config = config.ELECTRICITY_TARIFFS.get("grid_tariff", {}).get("hours", {})
    hour = dt.hour

    for period, hour_list in hours_config.items():
        if hour in hour_list:
            return period

    return "medium"  # fallback


def get_grid_tariff(dt: datetime) -> float:
    """
    Get the grid tariff [DKK/kWh] for a specific datetime.

    Looks up the current season (winter/summer) and time-of-use period
    from config.ELECTRICITY_TARIFFS.
    """
    tariff_config = config.ELECTRICITY_TARIFFS.get("grid_tariff", {})
    winter_months = tariff_config.get("winter_months", [1, 2, 3, 10, 11, 12])
    season = "winter" if dt.month in winter_months else "summer"
    period = get_current_period(dt)
    return tariff_config.get(season, {}).get(period, 0.30)


def calculate_full_price(
    spot_price_dkk_kwh: float,
    dt: datetime,
    include_vat: bool = True,
) -> float | None:
    """
    Calculate the full consumer electricity price [DKK/kWh].

    Adds grid tariff, system tariff, transmission tariff, electricity tax,
    and optionally VAT on top of the spot price.

    Args:
        spot_price_dkk_kwh: Spot price in DKK/kWh
        dt:                 Datetime for seasonal/hourly tariff lookup
        include_vat:        Whether to include 25% VAT (default True)
    """
    if spot_price_dkk_kwh is None:
        return None

    tariffs = config.ELECTRICITY_TARIFFS

    fixed = (
        tariffs.get("system_tariff", 0)
        + tariffs.get("transmission_tariff", 0)
        + tariffs.get("electricity_tax", 0)
    )

    price_ex_vat = spot_price_dkk_kwh + fixed + get_grid_tariff(dt)

    if include_vat:
        return round(price_ex_vat * (1 + tariffs.get("vat_rate", 0.25)), 4)

    return round(price_ex_vat, 4)


def get_tariff_schedule(dt: datetime = None, include_vat: bool = True) -> list[float]:
    """
    Return a 24-element tariff schedule (one value per hour 0-23).

    Returns fixed tariffs + grid tariffs only — does NOT include spot price
    since that varies day to day. Used by MPC to build price forecasts.

    Args:
        dt:          Date for seasonal tariff lookup (default: today)
        include_vat: Whether to include VAT (default True)
    """
    if dt is None:
        dt = datetime.now()

    tariffs_config = config.ELECTRICITY_TARIFFS
    fixed = (
        tariffs_config.get("system_tariff", 0)
        + tariffs_config.get("transmission_tariff", 0)
        + tariffs_config.get("electricity_tax", 0)
    )
    vat = 1 + tariffs_config.get("vat_rate", 0.25) if include_vat else 1.0

    schedule = []
    for hour in range(24):
        hour_dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        tariff = (fixed + get_grid_tariff(hour_dt)) * vat
        schedule.append(round(tariff, 4))

    return schedule


# =============================================================================
# SECTION 6: MPC HELPER FUNCTIONS
# =============================================================================

def get_price_horizon(
    prices: dict,
    steps: int,
    include_tariffs: bool = True,
    pad: bool = True,
) -> dict | None:
    """
    Extract a price forecast at 15-minute resolution for MPC use.

    Returns native 15-min data from the API starting at the current
    15-min boundary (e.g. at 12:46 → starts at 12:45).
    If actual prices don't cover the full horizon, pads with estimates
    using: avg_spot + tariff[hour].

    Args:
        prices:           Result from fetch_prices()
        steps:            Number of 15-min steps required (e.g., 96 for 24h)
        include_tariffs:  Add grid tariffs, taxes, VAT (default True)
        pad:              Pad short forecasts with estimates (default True)

    Returns:
        Dict with 15-min price arrays of length `steps` (if pad=True):
            time, dt_minutes, n_steps, price_spot, price_full
        or None if no data and pad=False.
    """
    times_str      = prices.get("time", [])
    prices_dkk_kwh = prices.get("price_dkk_kwh", [])

    if not times_str or not prices_dkk_kwh:
        logger.warning("No price data available")
        return _create_estimated_prices(steps, include_tariffs) if pad else None

    # Current 15-min boundary
    now = datetime.now()
    minute_rounded = (now.minute // 15) * 15
    start_time = now.replace(minute=minute_rounded, second=0, microsecond=0)

    # Extract native 15-min data from start_time onwards
    price_times: list = []
    price_spot:  list = []
    for t_str, spot in zip(times_str, prices_dkk_kwh):
        try:
            t = datetime.fromisoformat(t_str.replace("Z", "").replace("+00:00", ""))
            if t >= start_time and spot is not None:
                price_times.append(t)
                price_spot.append(spot)
        except Exception:
            continue

    if not price_spot:
        logger.warning("No future prices available")
        return _create_estimated_prices(steps, include_tariffs) if pad else None

    # Calculate full price with tariffs
    price_full = [
        calculate_full_price(spot, t, include_vat=True) if include_tariffs else spot
        for spot, t in zip(price_spot, price_times)
    ]

    # Pad to required number of steps if needed
    available = len(price_spot)
    if available < steps and pad:
        n_missing = steps - available
        avg_spot  = sum(price_spot) / len(price_spot)
        tariffs   = get_tariff_schedule(include_vat=include_tariffs)
        last_t    = price_times[-1]

        for i in range(n_missing):
            future_t = last_t + timedelta(minutes=15 * (i + 1))
            price_times.append(future_t)
            price_spot.append(avg_spot)
            price_full.append(
                round(avg_spot + tariffs[future_t.hour], 4)
                if include_tariffs else avg_spot
            )

        logger.warning(
            f"⚠️ Prices padded: {available}/{steps} steps available, "
            f"added {n_missing} estimated steps (avg_spot={avg_spot:.2f})"
        )

    n_steps = min(len(price_full), steps)
    logger.info(f"💰 Price horizon: {n_steps} steps (15-min) from {start_time.strftime('%H:%M')}")

    return {
        "time":       price_times[:n_steps],
        "dt_minutes": 15,
        "n_steps":    n_steps,
        "price_spot": price_spot[:n_steps],
        "price_full": price_full[:n_steps],
    }


def _create_estimated_prices(steps: int, include_tariffs: bool) -> dict:
    """
    Create a fully estimated price horizon when no API data is available.
    Returns native 15-min resolution starting from current 15-min boundary.
    Uses a default spot price of 1.0 DKK/kWh.
    """
    now            = datetime.now()
    minute_rounded = (now.minute // 15) * 15
    start_time     = now.replace(minute=minute_rounded, second=0, microsecond=0)
    default_spot   = 1.0
    tariffs        = get_tariff_schedule(include_vat=include_tariffs)

    price_times = [start_time + timedelta(minutes=15 * i) for i in range(steps)]
    price_spot  = [default_spot] * steps
    price_full  = [
        round(default_spot + tariffs[t.hour], 4) if include_tariffs else default_spot
        for t in price_times
    ]

    logger.warning(f"⚠️ No prices available, using estimates (spot={default_spot} DKK/kWh)")

    return {
        "time":       price_times,
        "dt_minutes": 15,
        "n_steps":    steps,
        "price_spot": price_spot,
        "price_full": price_full,
    }


def prices_to_numpy(horizon: dict) -> dict:
    """
    Convert a price horizon dict to numpy arrays.

    Args:
        horizon: Result from get_price_horizon()

    Returns:
        Same dict with numeric lists replaced by numpy arrays.
    """
    try:
        import numpy as np

        result = {
            "time":       horizon["time"],
            "dt_minutes": horizon["dt_minutes"],
            "n_steps":    horizon["n_steps"],
        }

        for key in ["price_spot", "price_full", "price_full_ex_vat"]:
            if key in horizon:
                result[key] = np.array([
                    v if v is not None else np.nan
                    for v in horizon[key]
                ])

        return result

    except ImportError:
        logger.warning("NumPy not available, returning raw dict")
        return horizon