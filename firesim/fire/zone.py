"""The hot smoke layer of a two-zone room model.

The layer is tracked by its volume and mass at near-constant pressure, which together give
its temperature. Below it is clean air at ambient temperature.
"""

import math
from dataclasses import dataclass, field

from firesim.fire.physics import (
    ENTHALPY_PER_M3,
    HEAT_PER_KG_O2,
    O2_AMB,
    RHO_AMB,
    SIGMA,
    T_AMB,
    solve_vents,
)

WALL_THERMAL_INERTIA = 2.0  # k*rho*c of plastered masonry, (kW/m²/K)² s
GAS_CONVECTION = 0.015  # kW/m²/K between smoke and walls
MAX_GAS_K = 1500.0
SOOT_EXTINCTION = 8700.0  # m²/kg
MAX_VISIBILITY = 30.0  # m
CO2_PER_KG_O2 = 1.1  # kg of CO2 produced per kg of oxygen consumed


@dataclass
class SmokeLayer:
    area: float  # floor area, m²
    height: float  # ceiling height, m
    perimeter: float  # m
    volume: float = field(init=False)
    mass: float = field(init=False)
    o2: float = field(init=False)  # kg
    co2: float = 0.0
    co: float = 0.0
    soot: float = 0.0
    floor_dp: float = 0.0
    inflow: float = 0.0  # m³/s of outside air entering the room

    def __post_init__(self) -> None:
        self.volume = 0.01 * self.room_volume
        self.mass = RHO_AMB * self.volume
        self.o2 = O2_AMB * self.mass

    @property
    def room_volume(self) -> float:
        return self.area * self.height

    @property
    def interface(self) -> float:
        """Height of the bottom of the smoke layer above the floor, m."""
        return max(self.height - self.volume / self.area, 0.0)

    @property
    def temperature(self) -> float:
        return min(max(RHO_AMB * T_AMB * self.volume / self.mass, T_AMB), MAX_GAS_K)

    @property
    def density(self) -> float:
        return RHO_AMB * T_AMB / self.temperature

    @property
    def o2_fraction(self) -> float:
        return max(self.o2 / self.mass, 0.0)

    @property
    def visibility(self) -> float:
        extinction = SOOT_EXTINCTION * self.soot / self.volume
        return min(MAX_VISIBILITY, 3.0 / extinction) if extinction > 0 else MAX_VISIBILITY

    @property
    def co_ppm(self) -> float:
        return self.co / self.mass * 28.97 / 28.01 * 1e6

    @property
    def o2_pct(self) -> float:
        return self.o2_fraction * 28.97 / 32.0 * 100.0

    @property
    def co2_pct(self) -> float:
        return self.co2 / self.mass * 28.97 / 44.01 * 100.0

    def lower_air_supply(self, dt: float) -> float:
        """kg/s of clean air the plumes can draw from below the layer."""
        return RHO_AMB * ((self.room_volume - self.volume) / dt + self.inflow)

    def advance(
        self,
        t: float,
        dt: float,
        strips: list[tuple[float, float]],
        *,
        entrained: float,
        q_total: float,
        q_convective: float,
        fuel_rate: float = 0.0,
        co_rate: float = 0.0,
        soot_rate: float = 0.0,
    ) -> None:
        """Step the layer forward. Rates are per second; heat is in kW."""
        interface, temperature, density = self.interface, self.temperature, self.density

        # Heat lost to the ceiling and the walls the smoke touches: gas-side film in series
        # with conduction into a thermally thick wall.
        wall_area = self.area + self.perimeter * (self.height - interface)
        h_solid = math.sqrt(WALL_THERMAL_INERTIA / max(t, 1.0))
        h_gas = GAS_CONVECTION + 0.8 * SIGMA * (temperature**2 + T_AMB**2) * (temperature + T_AMB)
        q_wall = wall_area * (temperature - T_AMB) / (1.0 / h_gas + 1.0 / h_solid)
        expansion = (q_convective - q_wall) / ENTHALPY_PER_M3

        flows = solve_vents(strips, interface, density, expansion, self.floor_dp)
        self.floor_dp, self.inflow = flows.floor_dp, flows.inflow
        out_fraction = min(density * flows.out_upper * dt / self.mass, 0.5)
        out_mass = out_fraction * self.mass

        self.volume += (entrained / RHO_AMB + expansion) * dt - out_mass / density
        self.mass += (entrained + fuel_rate) * dt - out_mass
        burnt_o2 = q_total / HEAT_PER_KG_O2
        self.o2 = max(self.o2 * (1 - out_fraction) + (entrained * O2_AMB - burnt_o2) * dt, 0.0)
        self.co2 = self.co2 * (1 - out_fraction) + CO2_PER_KG_O2 * burnt_o2 * dt
        self.co = self.co * (1 - out_fraction) + co_rate * dt
        self.soot = self.soot * (1 - out_fraction) + soot_rate * dt

        self.volume = min(max(self.volume, 0.005 * self.room_volume), self.room_volume)
        # Keep the layer between ambient temperature and the cap.
        self.mass = min(self.mass, RHO_AMB * self.volume)
        self.mass = max(self.mass, RHO_AMB * T_AMB * self.volume / MAX_GAS_K)
