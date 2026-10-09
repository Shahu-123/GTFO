"""Textbook relations used by the zone model: plume entrainment, vent flow, radiation."""

import math
from dataclasses import dataclass

from firesim.schema import RoomObject

T_AMB = 293.15  # K
RHO_AMB = 1.204  # kg/m³
CP = 1.005  # kJ/kg/K
G = 9.81
SIGMA = 5.67e-11  # kW/m²/K⁴
O2_AMB = 0.233  # mass fraction
HEAT_PER_KG_O2 = 13_100.0  # kJ released per kg of oxygen consumed
DISCHARGE = 0.7
# rho * cp * T is constant for air at constant pressure. Dividing a heat rate by it gives
# the rate at which the gas expands.
ENTHALPY_PER_M3 = RHO_AMB * CP * T_AMB


def plume_entrainment(q_kw: float, z: float) -> float:
    """McCaffrey plume: kg/s of gas drawn in below height z (m) above a fire of q_kw."""
    if q_kw <= 0 or z <= 0:
        return 0.0
    zq = z / q_kw**0.4
    if zq < 0.08:
        ratio = 0.011 * zq**0.566
    elif zq < 0.20:
        ratio = 0.026 * zq**0.909
    else:
        ratio = 0.124 * zq**1.895
    return ratio * q_kw


def oxygen_limit_factor(o2_mass_fraction: float, lower_limit: float = 0.15) -> float:
    """Smooth cut-off of burning as oxygen nears the lower limit (as in CFAST)."""
    return (math.tanh(800.0 * (o2_mass_fraction - lower_limit) - 4.0) + 1.0) / 2.0


@dataclass(frozen=True)
class Vent:
    width: float
    bottom: float  # m above the floor
    top: float

    def strips(self) -> list[tuple[float, float]]:
        """(mid height, area) slices used to integrate flow over the vent height."""
        height = self.top - self.bottom
        count = max(2, min(24, math.ceil(height / 0.1)))
        dz = height / count
        return [(self.bottom + (i + 0.5) * dz, self.width * dz) for i in range(count)]


@dataclass(frozen=True)
class VentFlows:
    out_upper: float  # m³/s of smoke leaving
    out_lower: float  # m³/s of lower-layer air leaving
    inflow: float  # m³/s of outside air entering
    floor_dp: float  # Pa, inside minus outside at floor level


def _flows(
    strips: list[tuple[float, float]], floor_dp: float, layer_height: float, rho_upper: float
) -> tuple[float, float, float]:
    out_upper = out_lower = inflow = 0.0
    buoyancy = G * (RHO_AMB - rho_upper)
    for z, area in strips:
        above = z - layer_height
        dp = floor_dp + buoyancy * above if above > 0 else floor_dp
        if dp > 0:
            if above > 0:
                out_upper += DISCHARGE * math.sqrt(2.0 * dp / rho_upper) * area
            else:
                out_lower += DISCHARGE * math.sqrt(2.0 * dp / RHO_AMB) * area
        elif dp < 0:
            inflow += DISCHARGE * math.sqrt(-2.0 * dp / RHO_AMB) * area
    return out_upper, out_lower, inflow


def solve_vents(
    strips: list[tuple[float, float]],
    layer_height: float,
    rho_upper: float,
    expansion: float,
    guess: float = 0.0,
) -> VentFlows:
    """Find the floor pressure at which net outflow equals the gas expansion rate (m³/s).

    The room is at near-constant pressure, so whatever volume the fire adds by heating the
    gas must leave through the openings. Hot smoke flows out high, outside air comes in low.
    """
    if not strips:
        return VentFlows(0.0, 0.0, 0.0, 0.0)

    def imbalance(dp: float) -> float:
        out_upper, out_lower, inflow = _flows(strips, dp, layer_height, rho_upper)
        return out_upper + out_lower - inflow - expansion

    span = 5.0
    lo, hi = guess - span, guess + span
    while imbalance(lo) > 0 and span < 1e6:
        span *= 4.0
        lo = guess - span
    while imbalance(hi) < 0 and span < 1e6:
        span *= 4.0
        hi = guess + span
    while hi - lo > 1e-7:
        mid = 0.5 * (lo + hi)
        if imbalance(mid) > 0:
            hi = mid
        else:
            lo = mid
    dp = 0.5 * (lo + hi)
    return VentFlows(*_flows(strips, dp, layer_height, rho_upper), dp)


def point_source_flux(q_kw: float, radiative_fraction: float, distance: float) -> float:
    """Radiant flux (kW/m²) at distance from a fire treated as a point source."""
    r = max(distance, 0.2)
    return radiative_fraction * q_kw / (4.0 * math.pi * r * r)


def _corner_view_factor(a: float, b: float, c: float) -> float:
    """Element to a parallel a x b rectangle whose corner is directly above it at height c."""
    if a <= 0 or b <= 0:
        return 0.0
    ac, bc = math.hypot(a, c), math.hypot(b, c)
    return (a / ac * math.atan(b / ac) + b / bc * math.atan(a / bc)) / (2.0 * math.pi)


def layer_view_factor(
    bounds: tuple[float, float, float, float], x: float, y: float, gap: float
) -> float:
    """View factor from an upward-facing point to the smoke layer base, a rectangle spanning
    bounds = (xmin, ymin, xmax, ymax) at height gap above the point."""
    if gap <= 0.01:
        return 1.0
    xmin, ymin, xmax, ymax = bounds
    left, right = max(x - xmin, 0.0), max(xmax - x, 0.0)
    front, back = max(y - ymin, 0.0), max(ymax - y, 0.0)
    return sum(_corner_view_factor(a, b, gap) for a in (left, right) for b in (front, back))


def layer_emissivity(thickness: float) -> float:
    return 1.0 - math.exp(-1.0 * max(thickness, 0.0))


def distance_to_object(x: float, y: float, z: float, obj: RoomObject) -> float:
    """Distance from a point to the nearest point of an object's bounding box."""
    angle = -math.radians(obj.rotation_deg)
    dx, dy = x - obj.position.x, y - obj.position.y
    lx = dx * math.cos(angle) - dy * math.sin(angle)
    ly = dx * math.sin(angle) + dy * math.cos(angle)
    ox = max(abs(lx) - obj.size.width / 2.0, 0.0)
    oy = max(abs(ly) - obj.size.depth / 2.0, 0.0)
    oz = max(obj.position.z - z, z - (obj.position.z + obj.size.height), 0.0)
    return math.sqrt(ox * ox + oy * oy + oz * oz)
