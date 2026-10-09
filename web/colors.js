// Colour scales shared by the 3D floor overlay, the 2D plans and their legends.

// Seconds until a spot is too hot to be in: red at once, green if it takes minutes.
export const HEAT_STOPS = [[0, '#d92b2b'], [30, '#e5532b'], [60, '#f08a24'], [120, '#f2d13c'], [240, '#3fb56b']];
// Harm to a person, 0 unharmed to 1 does not get out.
export const HARM_STOPS = [[0, '#3fb56b'], [0.1, '#f2d13c'], [0.3, '#f08a24'], [0.6, '#d92b2b'], [1, '#7a1010']];

const channels = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

function ramp(stops, value) {
  const v = Math.min(Math.max(value, stops[0][0]), stops.at(-1)[0]);
  const i = Math.max(1, stops.findIndex(([at]) => v <= at));
  const [v0, c0] = stops[i - 1];
  const [v1, c1] = stops[i];
  const mix = (v - v0) / (v1 - v0);
  const [a, b] = [channels(c0), channels(c1)];
  return `#${a.map((x, k) => Math.round(x + (b[k] - x) * mix).toString(16).padStart(2, '0')).join('')}`;
}

export const heatColor = (seconds) => ramp(HEAT_STOPS, seconds);
export const harmColor = (harm) => ramp(HARM_STOPS, harm);

// CSS gradient with the stops placed where their values fall between the ends of the scale.
export function gradient(stops) {
  const [min, max] = [stops[0][0], stops.at(-1)[0]];
  const parts = stops.map(([v, c]) => `${c} ${(((v - min) / (max - min)) * 100).toFixed(1)}%`);
  return `linear-gradient(90deg, ${parts.join(', ')})`;
}
