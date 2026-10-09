"""Fractional effective dose (FED) rates, after ISO 13571 and Purser (SFPE Handbook).

A dose of 1.0 is the point at which an average adult is expected to be incapacitated.
ISO 13571 suggests 0.3 as a conservative limit that also protects more vulnerable people.
Hydrogen cyanide and irritant gases are not modelled, so the gas dose is an underestimate
for fires in foams and synthetic fabrics.
"""

import math

INCAPACITATION = 1.0
VULNERABLE_LIMIT = 0.3
TOLERABLE_RADIANT_FLUX = 2.5  # kW/m², tolerable indefinitely below this
TOLERABLE_AIR_TEMP = 40.0  # °C


def asphyxiant_rate(co_ppm: float, co2_pct: float, o2_pct: float) -> float:
    """Dose per second from breathing gas with this composition."""
    hyperventilation = math.exp(co2_pct / 5.0)  # CO2 makes people breathe faster
    carbon_monoxide = co_ppm / 35_000.0  # per minute
    low_oxygen = 1.0 / math.exp(8.13 - 0.54 * (20.9 - o2_pct)) if o2_pct < 19.0 else 0.0
    return (carbon_monoxide * hyperventilation + low_oxygen) / 60.0


def heat_rate(radiant_flux: float, air_temp_c: float) -> float:
    """Dose per second from radiant heat on the skin (kW/m²) and hot air (°C)."""
    radiant = radiant_flux**1.56 / 6.9 if radiant_flux > TOLERABLE_RADIANT_FLUX else 0.0
    convective = air_temp_c**3.4 / 5.0e7 if air_temp_c > TOLERABLE_AIR_TEMP else 0.0
    return (radiant + convective) / 60.0
