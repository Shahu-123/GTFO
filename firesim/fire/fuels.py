"""Burning behaviour of room objects.

The values are order-of-magnitude figures from the fire engineering literature (SFPE
Handbook furniture calorimetry, NFPA 72 growth classes, Babrauskas' 10/20/40 kW/m² ignition
classes, Tewarson's yields). They are calibration constants, not measurements of the user's
furniture.
"""

from dataclasses import dataclass

from firesim.schema import FuelClass, RoomObject

# t² growth coefficients in kW/s² (NFPA 72).
MEDIUM = 0.01172
FAST = 0.0469

# Fraction of an item's energy released before it starts to die down.
DECAY_START = 0.7
# Shortest time an item can take to burn through its fuel at peak output, s.
MIN_BURN_TIME = 120.0


@dataclass(frozen=True)
class FuelProps:
    growth: float  # kW/s²
    hrrpua: float  # peak kW per m² of exposed bounding surface
    heat_of_combustion: float  # MJ/kg, effective
    bulk_density: float  # kg of fuel per m³ of bounding box
    soot_yield: float  # kg/kg
    co_yield: float  # kg/kg when well ventilated
    radiative_fraction: float
    ignition_flux: float  # kW/m² that ignites it


FUELS: dict[FuelClass, FuelProps | None] = {
    FuelClass.UPHOLSTERED_FOAM: FuelProps(FAST, 350, 25, 30, 0.10, 0.03, 0.40, 20),
    FuelClass.MATTRESS: FuelProps(FAST, 300, 25, 40, 0.10, 0.03, 0.40, 20),
    FuelClass.WOOD: FuelProps(MEDIUM, 150, 13, 60, 0.015, 0.005, 0.30, 40),
    FuelClass.TEXTILE: FuelProps(FAST, 150, 18, 15, 0.03, 0.01, 0.30, 10),
    FuelClass.PAPER: FuelProps(MEDIUM, 150, 13, 100, 0.01, 0.005, 0.30, 10),
    FuelClass.PLASTIC: FuelProps(FAST, 400, 30, 50, 0.06, 0.03, 0.40, 20),
    FuelClass.ELECTRONICS: FuelProps(MEDIUM, 250, 25, 80, 0.10, 0.04, 0.40, 20),
    FuelClass.OTHER: FuelProps(MEDIUM, 150, 18, 40, 0.04, 0.02, 0.35, 20),
    FuelClass.NON_COMBUSTIBLE: None,
}


@dataclass(frozen=True)
class BurnCurve:
    """Heat release demand of one item against its own burn clock tau (seconds).

    Grows as growth * tau² to the peak, holds until DECAY_START of the energy is released,
    then falls linearly to zero as the rest burns. The clock runs slower than real time
    when the fire is short of oxygen.
    """

    growth: float
    peak_kw: float
    energy_kj: float
    growth_end: float  # tau when growth stops
    decay_start: float  # tau when decay starts
    decay_end: float  # tau when the item is burnt out
    decay_from_kw: float

    def demand(self, tau: float) -> float:
        if tau <= 0 or tau >= self.decay_end:
            return 0.0
        if tau < self.growth_end:
            return self.growth * tau * tau
        if tau < self.decay_start:
            return self.peak_kw
        remaining = (self.decay_end - tau) / (self.decay_end - self.decay_start)
        return self.decay_from_kw * remaining


def burn_curve(obj: RoomObject, props: FuelProps) -> BurnCurve:
    w, d, h = obj.size.width, obj.size.depth, obj.size.height
    exposed_area = w * d + 2 * (w + d) * h
    energy = props.bulk_density * w * d * h * props.heat_of_combustion * 1000.0
    # Thin or sparse items (rugs, cables, curtains) have too little fuel to sustain the
    # heat release their surface area alone would suggest.
    peak = min(props.hrrpua * exposed_area, energy / MIN_BURN_TIME)

    before_decay = DECAY_START * energy
    to_peak = (peak / props.growth) ** 0.5
    growth_energy = props.growth * to_peak**3 / 3.0
    if growth_energy >= before_decay:
        # Too little fuel to reach the nominal peak: decay begins during growth.
        growth_end = (3.0 * before_decay / props.growth) ** (1.0 / 3.0)
        decay_start = growth_end
        peak = props.growth * growth_end**2
    else:
        growth_end = to_peak
        decay_start = to_peak + (before_decay - growth_energy) / peak

    decay_end = decay_start + 2.0 * (energy - before_decay) / peak
    return BurnCurve(
        growth=props.growth,
        peak_kw=peak,
        energy_kj=energy,
        growth_end=growth_end,
        decay_start=decay_start,
        decay_end=decay_end,
        decay_from_kw=peak,
    )
