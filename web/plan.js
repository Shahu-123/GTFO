// 2D plan of a room with one of the risk maps on it. Only the main furniture and the
// high-risk items are drawn, each with a label. Drawn in centimetres (very small font
// sizes render badly in SVG), with y flipped so the plan reads the same way up as the room.

import { harmColor, heatColor } from './colors.js';

const NS = 'http://www.w3.org/2000/svg';
const K = 100; // drawing units per metre
const PAD = 0.45 * K; // around the room, for door and window labels
const FONT = 0.115 * K;
const X = (x) => x * K;
const Y = (y) => -y * K;

function node(tag, attrs = {}, text = null) {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (text !== null) el.textContent = text;
  return el;
}

const titleCase = (text) => text.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase());

// Outline of an object's footprint, with its full name as a tooltip.
function footprint(obj, attrs, tooltip = obj.label) {
  const angle = (obj.rotation_deg * Math.PI) / 180;
  const [cos, sin] = [Math.cos(angle), Math.sin(angle)];
  const [hw, hd] = [obj.size.width / 2, obj.size.depth / 2];
  const points = [[-hw, -hd], [hw, -hd], [hw, hd], [-hw, hd]]
    .map(([lx, ly]) => `${X(obj.position.x + lx * cos - ly * sin)},${Y(obj.position.y + lx * sin + ly * cos)}`)
    .join(' ');
  const polygon = node('polygon', { points, ...attrs });
  polygon.append(node('title', {}, tooltip));
  return polygon;
}

function signedArea(floor) {
  return floor.reduce((sum, p, i) => {
    const q = floor[(i + 1) % floor.length];
    return sum + (p.x * q.y - q.x * p.y) / 2;
  }, 0);
}

// Places labels so that they do not sit on top of each other: each new label moves down
// a line at a time until it is clear of the ones already placed.
function labeller(layer) {
  const placed = [];
  return (text, x, y, className) => {
    const width = text.length * FONT * 0.56;
    let top = y - FONT / 2;
    for (let tries = 0; tries < 6; tries += 1) {
      const clash = placed.some((b) => Math.abs(b.x - x) < (b.width + width) / 2 && Math.abs(b.top - top) < FONT * 1.1);
      if (!clash) break;
      top += FONT * 1.15;
    }
    placed.push({ x, top, width });
    layer.append(node('text', { x, y: top + FONT * 0.85, class: className, 'font-size': FONT }, text));
  };
}

export function drawPlan(svg, room, report, kind) {
  const xs = room.floor.map((p) => p.x);
  const ys = room.floor.map((p) => p.y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  svg.replaceChildren();
  svg.setAttribute('viewBox', `${X(minX) - PAD} ${Y(maxY) - PAD} ${X(maxX - minX) + 2 * PAD} ${X(maxY - minY) + 2 * PAD}`);

  // The map itself: one square per floor cell.
  const { x0, y0, cell, nx, values } = report[kind];
  const colorOf = kind === 'heat' ? heatColor : harmColor;
  const cells = node('g');
  values.forEach((value, k) => {
    if (value === null) return;
    const [i, j] = [k % nx, Math.floor(k / nx)];
    cells.append(node('rect', {
      x: X(x0 + i * cell), y: Y(y0 + (j + 1) * cell), width: X(cell) + 0.5, height: X(cell) + 0.5, fill: colorOf(value),
    }));
  });
  svg.append(cells);

  const byId = new Map(room.objects.map((obj) => [obj.id, obj]));
  const risky = new Set(report.high_risk_items);
  const resting = kind === 'danger' ? report.resting : [];
  const restingIds = new Set(resting.map((place) => place.object_id));
  const shapes = node('g');
  const labels = node('g', { class: 'labels' });
  const label = labeller(labels);

  // A bed frame under a mattress that is drawn as a resting place would only repeat it.
  const underResting = (obj) => resting.some((place) => {
    const on = byId.get(place.object_id);
    return on && Math.hypot(on.position.x - obj.position.x, on.position.y - obj.position.y) < 0.3;
  });

  // Main furniture first, so the risky items are drawn over it.
  for (const id of report.main_items) {
    const obj = byId.get(id);
    if (!obj || risky.has(id) || restingIds.has(id) || underResting(obj)) continue;
    shapes.append(footprint(obj, { class: 'item' }));
    label(titleCase(obj.category), X(obj.position.x), Y(obj.position.y), 'item-label');
  }
  // Where someone sleeps, coloured by how a sleeper there fares.
  for (const place of resting) {
    const obj = byId.get(place.object_id);
    if (!obj) continue;
    const tooltip = `${obj.label}: average harm to a sleeper ${place.harm.toFixed(2)}`;
    shapes.append(footprint(obj, { class: 'resting', fill: harmColor(place.harm) }, tooltip));
    label(`Asleep on the bed: ${place.harm.toFixed(2)}`, X(obj.position.x), Y(obj.position.y), 'item-label strong');
  }
  for (const id of report.high_risk_items) {
    const obj = byId.get(id);
    if (!obj) continue;
    shapes.append(footprint(obj, { class: 'item risky' }));
    label(titleCase(obj.category), X(obj.position.x), Y(obj.position.y), 'risky-label');
  }
  svg.append(shapes);

  // Walls, then doors and windows drawn over them and labelled just outside.
  svg.append(node('polygon', { points: room.floor.map((p) => `${X(p.x)},${Y(p.y)}`).join(' '), class: 'walls' }));
  const outward = signedArea(room.floor) > 0 ? 1 : -1;
  for (const opening of room.openings) {
    const a = room.floor[opening.wall_index];
    const b = room.floor[(opening.wall_index + 1) % room.floor.length];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    const [ux, uy] = [(b.x - a.x) / length, (b.y - a.y) / length];
    const [sx, sy] = [a.x + ux * opening.offset, a.y + uy * opening.offset];
    const [ex, ey] = [sx + ux * opening.width, sy + uy * opening.width];
    svg.append(node('line', { x1: X(sx), y1: Y(sy), x2: X(ex), y2: Y(ey), class: `opening ${opening.kind}` }));
    const name = { door: 'Door', window: 'Window', open_passage: 'Opening' }[opening.kind];
    const [mx, my] = [(sx + ex) / 2 + uy * outward * 0.2, (sy + ey) / 2 - ux * outward * 0.2];
    label(name, X(mx), Y(my), `opening-label ${opening.kind}`);
  }

  if (report.route.length > 1) {
    const points = report.route.map((p) => `${X(p.x)},${Y(p.y)}`).join(' ');
    svg.append(node('polyline', { points, class: 'route-edge' }), node('polyline', { points, class: 'route' }));
  }
  svg.append(labels);
}
