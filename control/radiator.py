# =============================================================================
# RADIATOR - Radiator Physics and Calculations
# =============================================================================
#
# Provides a Radiator class for modelling panel radiators (EN 442).
#
# Usage:
#   rad = Radiator(rad_type=22, height_mm=600, length_m=1.2)
#   q   = rad.output(t_supply=45, t_return=35, t_room=21)
#
# Multiple radiators per room:
#   rads = [Radiator(22, 600, 1.2), Radiator(11, 400, 0.8)]
#   q_total = sum(r.output(t_supply, t_return, t_room) for r in rads)
#
# Config-driven setup (see config.py ROOMS definition):
#   radiators = Radiator.from_config(config.ROOMS["living_room"]["radiators"])
#
# =============================================================================

import logging

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: EN 442 PERFORMANCE DATA
# =============================================================================

# Specific heat output [W/m] at reference ΔT = 50K (75/65/20°C flow conditions)
# Based on EN 442, averaged from market leaders (Purmo, Stelrad, Rio)
# Structure: {radiator_type: {height_mm: watts_per_meter}}
PERFORMANCE_MAP = {
    10: {300: 335,  400: 430,  500: 520,  600: 610,  900: 870},
    11: {300: 510,  400: 660,  500: 800,  600: 940,  900: 1330},
    21: {300: 740,  400: 930,  500: 1110, 600: 1290, 900: 1810},
    22: {300: 970,  400: 1220, 500: 1460, 600: 1690, 900: 2380},
    33: {300: 1390, 400: 1730, 500: 2060, 600: 2380, 900: 3350},
}

RADIATOR_TYPES = {
    10: "Single panel, no convector fins",
    11: "Single panel, single convector",
    21: "Double panel, single convector",
    22: "Double panel, double convector",
    33: "Triple panel, triple convector",
}


# =============================================================================
# SECTION 2: RADIATOR CLASS
# =============================================================================

class Radiator:
    """
    Models a single panel radiator using the EN 442 radiator equation:

        Q = UA × ΔT^n

    Where:
        Q  = Heat output [W]
        UA = Heat transfer coefficient [W/K^n], derived from EN 442 data
        ΔT = Mean radiator temperature − room temperature [K]
        n  = Radiator exponent (typically 1.3 for panel radiators)

    Args:
        rad_type:   Panel type (10, 11, 21, 22, 33)
        height_mm:  Height in mm (300, 400, 500, 600, 900 — or interpolated)
        length_m:   Length in meters
        n:          Radiator exponent (default 1.3)
        name:       Optional label for logging/debugging

    Example:
        >>> rad = Radiator(rad_type=22, height_mm=600, length_m=1.2)
        >>> rad.output(t_supply=45, t_return=35, t_room=21)
        713.6
    """

    def __init__(
        self,
        rad_type: int,
        height_mm: int | float,
        length_m: float,
        n: float = 1.3,
        name: str = "",
    ):
        if rad_type not in PERFORMANCE_MAP:
            raise ValueError(
                f"Unknown radiator type: {rad_type}. "
                f"Supported: {sorted(PERFORMANCE_MAP.keys())}"
            )

        self.rad_type  = rad_type
        self.height_mm = height_mm
        self.length_m  = length_m
        self.n         = n
        self.name      = name or f"Type{rad_type}-{int(height_mm)}mm-{length_m}m"

        # UA is fixed for a given radiator — compute once at instantiation
        self.ua = self._compute_ua()

        logger.debug(
            f"Radiator '{self.name}': type={rad_type}, "
            f"h={height_mm}mm, l={length_m}m, n={n}, UA={self.ua:.2f} W/K^n"
        )

    def _compute_ua(self) -> float:
        """Derive UA from EN 442 performance data at reference ΔT = 50K."""
        type_data      = PERFORMANCE_MAP[self.rad_type]
        sorted_heights = sorted(type_data.keys())

        # Specific output [W/m] at ΔT = 50K
        if self.height_mm in type_data:
            q_ref = type_data[self.height_mm]
        elif self.height_mm < sorted_heights[0]:
            logger.warning(
                f"Radiator '{self.name}': height {self.height_mm}mm below minimum "
                f"{sorted_heights[0]}mm, clamping."
            )
            q_ref = type_data[sorted_heights[0]]
        elif self.height_mm > sorted_heights[-1]:
            logger.warning(
                f"Radiator '{self.name}': height {self.height_mm}mm above maximum "
                f"{sorted_heights[-1]}mm, clamping."
            )
            q_ref = type_data[sorted_heights[-1]]
        else:
            # Linear interpolation between bracketing heights
            lower_h = max(h for h in sorted_heights if h <= self.height_mm)
            upper_h = min(h for h in sorted_heights if h >= self.height_mm)
            frac    = (self.height_mm - lower_h) / (upper_h - lower_h)
            q_ref   = type_data[lower_h] + (type_data[upper_h] - type_data[lower_h]) * frac

        # Total nominal output [W]
        Q_ref = q_ref * self.length_m

        # UA = Q / ΔT^n  at ΔT = 50K
        return Q_ref / (50 ** self.n)

    def output(self, t_supply: float, t_return: float, t_room: float) -> float:
        """
        Calculate heat output [W] given pipe and room temperatures.

        Args:
            t_supply: Supply pipe temperature [°C]
            t_return: Return pipe temperature [°C]
            t_room:   Room air temperature [°C]

        Returns:
            Heat output in Watts (0.0 if radiator is colder than room).
        """
        t_mean  = (t_supply + t_return) / 2
        delta_t = t_mean - t_room

        if delta_t <= 0:
            return 0.0

        return round(self.ua * (delta_t ** self.n), 1)

    def max_output(self, t_supply: float, t_room: float) -> float:
        """
        Estimate maximum output assuming supply = return (no flow loss).
        Useful as an upper bound estimate.
        """
        return self.output(t_supply, t_supply, t_room) * 0.5

    def nominal_output(self) -> float:
        """Nominal output at EN 442 reference conditions (75/65/20°C)."""
        return self.output(t_supply=75, t_return=65, t_room=20)

    def __repr__(self) -> str:
        return (
            f"Radiator(name='{self.name}', type={self.rad_type}, "
            f"h={self.height_mm}mm, l={self.length_m}m, "
            f"UA={self.ua:.2f}, Q_nom={self.nominal_output():.0f}W)"
        )

    @classmethod
    def from_config(cls, radiator_configs: list[dict]) -> list["Radiator"]:
        """
        Create a list of Radiator instances from a config list.

        Each dict should have keys: rad_type, height_mm, length_m, and
        optionally n and name.

        Example config:
            [
                {"rad_type": 22, "height_mm": 600, "length_m": 1.2},
                {"rad_type": 11, "height_mm": 400, "length_m": 0.8, "name": "extra"},
            ]
        """
        return [
            cls(
                rad_type=c["rad_type"],
                height_mm=c["height_mm"],
                length_m=c["length_m"],
                n=c.get("n", 1.3),
                name=c.get("name", ""),
            )
            for c in radiator_configs
        ]


# =============================================================================
# SECTION 3: SUPPLY TEMPERATURE CURVE
# =============================================================================

def supply_temp_from_outdoor(t_outdoor: float, curve: list[tuple] | None = None) -> float:
    """
    Estimate heat pump supply temperature from outdoor temperature using
    a piecewise linear heating curve.

    Args:
        t_outdoor: Current outdoor temperature [°C]
        curve:     List of (outdoor_temp, supply_temp) tuples, sorted by
                   outdoor_temp ascending. If None, reads from config.

    Returns:
        Estimated supply temperature [°C]

    Example:
        >>> supply_temp_from_outdoor(-5)
        50.0  # interpolated between (-15, 55) and (0, 45)
    """
    if curve is None:
        import config
        curve = config.SUPPLY_TEMP_CURVE

    # Sort by outdoor temp just in case
    curve = sorted(curve, key=lambda p: p[0])

    # Clamp to curve endpoints
    if t_outdoor <= curve[0][0]:
        return float(curve[0][1])
    if t_outdoor >= curve[-1][0]:
        return float(curve[-1][1])

    # Linear interpolation between bracketing points
    for i in range(len(curve) - 1):
        t0, s0 = curve[i]
        t1, s1 = curve[i + 1]
        if t0 <= t_outdoor <= t1:
            frac = (t_outdoor - t0) / (t1 - t0)
            return round(s0 + (s1 - s0) * frac, 1)

    return float(curve[-1][1])  # fallback


# =============================================================================
# SECTION 4: ROOM HELPERS
# =============================================================================

def total_output(
    radiators: list[Radiator],
    t_supply: float,
    t_return: float,
    t_room: float,
) -> float:
    """
    Sum heat output across all radiators in a room.

    Args:
        radiators: List of Radiator instances
        t_supply:  Supply pipe temperature [°C]
        t_return:  Return pipe temperature [°C]
        t_room:    Room air temperature [°C]

    Returns:
        Total heat output in Watts.
    """
    return sum(r.output(t_supply, t_return, t_room) for r in radiators)