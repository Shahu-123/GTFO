// Draws a Room (see firesim/schema.py) and plays back a FireRun (see firesim/fire/sim.py).
// The scene is built in room coordinates (metres, z up) inside a root group that is
// rotated into three.js's y-up frame.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { harmColor, heatColor } from './colors.js';
import { createFigure } from './figure.js';

const HEX = /^#[0-9a-f]{6}$/i;
const rad = (deg) => (deg * Math.PI) / 180;
const clamp = THREE.MathUtils.clamp;
const color = (value, fallback = '#9a9a9a') => new THREE.Color(HEX.test(value) ? value : fallback);

const CHAR = new THREE.Color('#17120e');
const GLOW = new THREE.Color('#ff5a14');
const SELECTED = new THREE.Color('#7a3a10');
const FLAME_COLORS = [new THREE.Color('#ffd98a'), new THREE.Color('#ff7414'), new THREE.Color('#9c1a04')];
const SMOKE_THIN = new THREE.Color('#8d8880');
const SMOKE_THICK = new THREE.Color('#1b1917');
const FLAMES_PER_OBJECT = 28;
// Distance covered in one full gait cycle, per posture (m).
const STRIDE = { upright: 1.4, crouch: 1.1, crawl: 0.9 };

function signedArea(floor) {
  return floor.reduce((sum, p, i) => {
    const q = floor[(i + 1) % floor.length];
    return sum + (p.x * q.y - q.x * p.y) / 2;
  }, 0);
}

function floorShape(room) {
  return new THREE.Shape(room.floor.map((p) => new THREE.Vector2(p.x, p.y)));
}

function buildFloor(room) {
  const mesh = new THREE.Mesh(
    new THREE.ShapeGeometry(floorShape(room)),
    new THREE.MeshStandardMaterial({ color: color(room.floor_color), roughness: 0.85, side: THREE.DoubleSide }),
  );
  mesh.receiveShadow = true;
  return mesh;
}

// Solid wall rectangles around the openings, as [u0, u1, v0, v1] in wall coordinates
// (u along the wall, v up).
function wallPanels(length, height, openings) {
  const panels = [];
  let cursor = 0;
  for (const o of [...openings].sort((a, b) => a.offset - b.offset)) {
    const u0 = clamp(o.offset, cursor, length);
    const u1 = clamp(o.offset + o.width, u0, length);
    const v0 = clamp(o.sill_height, 0, height);
    const v1 = clamp(o.sill_height + o.height, v0, height);
    panels.push([cursor, u0, 0, height], [u0, u1, 0, v0], [u0, u1, v1, height]);
    cursor = u1;
  }
  panels.push([cursor, length, 0, height]);
  return panels.filter(([u0, u1, v0, v1]) => u1 - u0 > 1e-3 && v1 - v0 > 1e-3);
}

function buildOpening(o, inward) {
  const group = new THREE.Group();
  group.position.set(o.offset, o.sill_height, 0);

  if (o.kind === 'door') {
    const leaf = new THREE.Mesh(
      new THREE.BoxGeometry(o.width, o.height, 0.04),
      new THREE.MeshStandardMaterial({ color: '#8a6a4a', roughness: 0.7 }),
    );
    leaf.position.set(o.width / 2, o.height / 2, 0);
    leaf.castShadow = true;
    const hinge = new THREE.Group();
    hinge.add(leaf);
    // Rotating about the wall's up axis by +90° swings local +u towards -z.
    if (o.is_open) hinge.rotation.y = rad(80) * -inward;
    group.add(hinge);
  } else if (o.kind === 'window') {
    const pane = new THREE.PlaneGeometry(o.width, o.height);
    const frame = new THREE.LineSegments(
      new THREE.EdgesGeometry(pane),
      new THREE.LineBasicMaterial({ color: '#f5f5f5' }),
    );
    frame.position.set(o.width / 2, o.height / 2, 0);
    group.add(frame);
    if (!o.is_open) {
      const glass = new THREE.Mesh(pane, new THREE.MeshStandardMaterial({
        color: '#9fd0ff', transparent: true, opacity: 0.3, roughness: 0.1, side: THREE.DoubleSide,
      }));
      glass.position.copy(frame.position);
      group.add(glass);
    }
  }

  if (o.leads_outside_room) {
    const threshold = new THREE.Mesh(
      new THREE.BoxGeometry(o.width, 0.012, 0.14),
      new THREE.MeshStandardMaterial({ color: '#4cc38a', emissive: '#2a8f5f' }),
    );
    threshold.position.set(o.width / 2, 0.006 - o.sill_height, 0);
    group.add(threshold);
  }
  return group;
}

function buildWalls(room) {
  const group = new THREE.Group();
  const n = room.floor.length;
  const ccw = signedArea(room.floor) > 0;
  // Each wall's local z is its edge direction crossed with up, which points out of a
  // counter-clockwise room. Drawing only the inward face keeps the interior visible.
  const material = new THREE.MeshStandardMaterial({
    color: color(room.wall_color, '#e6e1d6'), roughness: 0.95,
    side: ccw ? THREE.BackSide : THREE.FrontSide,
  });
  const up = new THREE.Vector3(0, 0, 1);

  room.floor.forEach((a, i) => {
    const b = room.floor[(i + 1) % n];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (length < 1e-3) return;
    const along = new THREE.Vector3(b.x - a.x, b.y - a.y, 0).normalize();
    const wall = new THREE.Group();
    wall.matrixAutoUpdate = false;
    wall.matrix
      .makeBasis(along, up, new THREE.Vector3().crossVectors(along, up))
      .setPosition(a.x, a.y, 0);

    const openings = room.openings.filter((o) => o.wall_index === i);
    for (const [u0, u1, v0, v1] of wallPanels(length, room.ceiling_height, openings)) {
      const panel = new THREE.Mesh(new THREE.PlaneGeometry(u1 - u0, v1 - v0), material);
      panel.position.set((u0 + u1) / 2, (v0 + v1) / 2, 0);
      panel.receiveShadow = true;
      wall.add(panel);
    }
    for (const o of openings) wall.add(buildOpening(o, ccw ? -1 : 1));
    group.add(wall);
  });

  const h = room.ceiling_height;
  const outline = [];
  room.floor.forEach((a, i) => {
    const b = room.floor[(i + 1) % n];
    outline.push(a.x, a.y, h, b.x, b.y, h, a.x, a.y, 0, a.x, a.y, h);
  });
  const lines = new THREE.BufferGeometry();
  lines.setAttribute('position', new THREE.Float32BufferAttribute(outline, 3));
  group.add(new THREE.LineSegments(lines, new THREE.LineBasicMaterial({ color: '#6f7783' })));
  return group;
}

function partGeometry(part) {
  const { width, depth, height } = part.size;
  if (part.shape === 'cylinder') {
    const g = new THREE.CylinderGeometry(0.5, 0.5, 1, 24);
    g.rotateX(Math.PI / 2); // stand along z
    return g.scale(width, depth, height);
  }
  if (part.shape === 'sphere') return new THREE.SphereGeometry(0.5, 24, 16).scale(width, depth, height);
  return new THREE.BoxGeometry(width, depth, height);
}

function buildObject(obj) {
  const group = new THREE.Group();
  group.position.set(obj.position.x, obj.position.y, obj.position.z);
  group.rotation.z = rad(obj.rotation_deg);
  group.userData.objectId = obj.id;

  const parts = obj.parts.length
    ? obj.parts
    : [{ shape: 'box', center: { x: 0, y: 0, z: obj.size.height / 2 }, size: obj.size, rotation_deg: 0, color: '#9a9a9a' }];
  for (const part of parts) {
    const mesh = new THREE.Mesh(
      partGeometry(part),
      new THREE.MeshStandardMaterial({ color: color(part.color), roughness: 0.8 }),
    );
    mesh.position.set(part.center.x, part.center.y, part.center.z);
    mesh.rotation.z = rad(part.rotation_deg);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.userData.baseColor = mesh.material.color.clone();
    group.add(mesh);
  }

  if (obj.is_ignition_source) {
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 16, 12),
      new THREE.MeshBasicMaterial({ color: '#ff7a2f' }),
    );
    marker.position.set(0, 0, obj.size.height + 0.2);
    marker.userData.marker = true;
    group.add(marker);
  }
  return group;
}

function flameTexture() {
  const size = 64;
  const canvas = Object.assign(document.createElement('canvas'), { width: size, height: size });
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, 'rgba(255,255,255,0.9)');
  gradient.addColorStop(0.25, 'rgba(255,255,255,0.5)');
  gradient.addColorStop(0.6, 'rgba(255,255,255,0.1)');
  gradient.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(canvas);
}

// Mean flame height above the fuel (Heskestad), from heat release in kW and footprint.
function flameHeight(hrr, obj) {
  const diameter = Math.sqrt((4 * obj.size.width * obj.size.depth) / Math.PI);
  return clamp(0.235 * hrr ** 0.4 - 1.02 * diameter, 0.2, 3);
}

// Ribbon lying on the floor along a list of {x, y} or Vector3 points.
function ribbon(points, colorValue) {
  const mesh = new THREE.Mesh(
    new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points), points.length * 4, 0.02, 6),
    new THREE.MeshBasicMaterial({ color: colorValue, transparent: true, opacity: 0.9 }),
  );
  mesh.renderOrder = 12; // after the smoke, like the figure
  return mesh;
}

// Value of a once-per-second series at time t, linearly interpolated.
function sample(series, t) {
  const i = clamp(Math.floor(t), 0, series.length - 1);
  const j = Math.min(i + 1, series.length - 1);
  return series[i] + (series[j] - series[i]) * clamp(t - i, 0, 1);
}

export function createViewer(canvas, onSelect) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#14161a');
  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 200);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI / 2 - 0.02;

  const sky = new THREE.HemisphereLight('#ffffff', '#3a3f48', 1.6);
  const sun = new THREE.DirectionalLight('#fff4e0', 2.2);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  const fireLight = new THREE.PointLight('#ff8a3c', 0, 0, 1.6);
  scene.add(sky, sun, sun.target);

  const flameMap = flameTexture();
  const clock = new THREE.Clock();
  let root = null;
  let room = null;
  let objects = new Map(); // id -> { obj, group, meshes, flames, burnt, hrr }
  let selected = null;
  let fire = null; // { run, smoke }
  let escape = null; // { run, travelled, trail }
  let risk = null; // group holding the danger map and the clear-air route
  const figure = createFigure();
  let fireTime = 0;
  let smokeVisible = true;

  function resize() {
    const { clientWidth: w, clientHeight: h } = canvas.parentElement;
    if (!w || !h) return; // hidden, for example on the other tab
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(canvas.parentElement);
  resize();

  function select(id) {
    selected = objects.has(id) ? id : null;
    applyFireState();
  }

  function disposeTree(node) {
    node.traverse((child) => {
      child.geometry?.dispose();
      child.material?.dispose();
    });
  }

  function show(newRoom, { keepCamera = false } = {}) {
    if (root) {
      figure.group.removeFromParent(); // reused across rooms, so keep it out of the disposal
      scene.remove(root);
      disposeTree(root);
    }
    room = newRoom;
    fire = null;
    escape = null;
    risk = null;
    root = new THREE.Group();
    root.rotation.x = -Math.PI / 2; // room (x, y, z up) -> three (x, z up, -y)
    root.add(buildFloor(room), buildWalls(room), fireLight, figure.group);
    fireLight.intensity = 0;
    figure.group.visible = false;
    objects = new Map();
    selected = null;
    for (const obj of room.objects) {
      const group = buildObject(obj);
      const meshes = group.children.filter((child) => child.isMesh && !child.userData.marker);
      objects.set(obj.id, { obj, group, meshes, flames: null, burnt: 0, hrr: 0 });
      root.add(group);
    }
    scene.add(root);

    const xs = room.floor.map((p) => p.x);
    const ys = room.floor.map((p) => p.y);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), room.ceiling_height);

    if (!keepCamera) {
      controls.target.set(cx, room.ceiling_height / 3, -cy);
      camera.position.set(cx + span * 0.9, room.ceiling_height + span * 0.9, -cy + span * 1.2);
      controls.update();
    }

    sun.position.set(cx + span * 0.4, room.ceiling_height + span * 1.5, -cy + span * 0.6);
    sun.target.position.set(cx, 0, -cy);
    const s = span * 1.2;
    Object.assign(sun.shadow.camera, { left: -s, right: s, top: s, bottom: -s, near: 0.1, far: span * 6 });
    sun.shadow.camera.updateProjectionMatrix();
  }

  function buildFlames(entry) {
    const { obj, group } = entry;
    const tall = obj.size.height > 1.2;
    const flames = [];
    for (let i = 0; i < FLAMES_PER_OBJECT; i += 1) {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: flameMap, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true,
      }));
      sprite.renderOrder = 11; // after the smoke, so fire spread stays visible through it
      sprite.userData = {
        phase: Math.random(),
        speed: 0.9 + Math.random() * 0.8,
        size: 0.7 + Math.random() * 0.6,
        x: (Math.random() - 0.5) * obj.size.width * 0.8,
        y: (Math.random() - 0.5) * obj.size.depth * 0.8,
        // Tall items burn up their faces; low ones burn from the top surface.
        base: obj.size.height * (tall ? 0.3 + Math.random() * 0.7 : 1),
      };
      sprite.visible = false;
      group.add(sprite);
      flames.push(sprite);
    }
    return flames;
  }

  function setFire(run, escapeRun = null) {
    for (const entry of objects.values()) {
      entry.flames?.forEach((sprite) => {
        entry.group.remove(sprite);
        sprite.material.dispose();
      });
      Object.assign(entry, { flames: null, burnt: 0, hrr: 0, burn: null, burntSeries: null });
    }
    if (fire?.smoke) {
      root.remove(fire.smoke);
      disposeTree(fire.smoke);
    }
    if (escape?.trail) {
      root.remove(escape.trail);
      disposeTree(escape.trail);
    }
    fire = null;
    escape = null;
    fireTime = 0;
    figure.group.visible = false;
    if (!run) {
      applyFireState();
      return;
    }
    if (escapeRun) escape = buildEscape(escapeRun);

    for (const [id, burn] of Object.entries(run.objects)) {
      const entry = objects.get(id);
      if (!entry) continue;
      let released = 0;
      entry.burn = burn;
      // Fraction of the item's energy released by each second, for charring.
      entry.burntSeries = burn.hrr_kw.map((kw) => (released += kw) / burn.energy_kj);
    }
    // Unit-depth slab of the floor outline, stretched each frame to the smoke layer.
    const smoke = new THREE.Mesh(
      new THREE.ExtrudeGeometry(floorShape(room), { depth: 1, bevelEnabled: false }),
      new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, opacity: 0 }),
    );
    smoke.renderOrder = 10;
    root.add(smoke);
    fire = { run, smoke };
    setTime(0);
  }

  // Distance walked up to each sample (for the gait) and a ribbon along the route taken.
  function buildEscape(run) {
    const travelled = [0];
    const points = [new THREE.Vector3(run.x[0], run.y[0], 0.03)];
    for (let i = 1; i < run.x.length; i += 1) {
      travelled.push(travelled[i - 1] + Math.hypot(run.x[i] - run.x[i - 1], run.y[i] - run.y[i - 1]));
      const point = new THREE.Vector3(run.x[i], run.y[i], 0.03);
      if (point.distanceTo(points.at(-1)) > 0.08) points.push(point);
    }
    const trail = points.length > 1 ? ribbon(points, '#4cc3ff') : null;
    if (trail) root.add(trail);
    return { run, travelled, trail };
  }

  function movePerson(t) {
    const { run, travelled } = escape;
    const last = run.x.length - 1;
    const at = clamp(t / run.dt, 0, last);
    const i = Math.floor(at);
    const j = Math.min(i + 1, last);
    const mix = at - i;
    const lerp = (series) => series[i] + (series[j] - series[i]) * mix;
    const turn = ((run.heading_deg[j] - run.heading_deg[i] + 540) % 360) - 180;
    const posture = run.posture[i];

    const out = run.summary.escaped_s !== null && t >= run.summary.escaped_s;
    figure.group.visible = !out;
    figure.group.position.set(lerp(run.x), lerp(run.y), lerp(run.z));
    figure.group.rotation.z = rad(run.heading_deg[i] + turn * mix);
    const phase = (lerp(travelled) / (STRIDE[posture] ?? 1.4)) * 2 * Math.PI;
    figure.setPose(posture, phase, travelled[j] - travelled[i] > 0.01);
  }

  function setTime(t) {
    if (!fire) return;
    fireTime = t;
    if (escape) movePerson(t);
    for (const entry of objects.values()) {
      entry.hrr = entry.burn ? sample(entry.burn.hrr_kw, t) : 0;
      entry.burnt = entry.burntSeries ? clamp(sample(entry.burntSeries, t), 0, 1) : 0;
      if (entry.hrr > 0 && !entry.flames) entry.flames = buildFlames(entry);
    }
    applyFireState();

    const { run, smoke } = fire;
    const layer = sample(run.layer_height_m, t);
    const density = 3 / sample(run.visibility_m, t); // extinction coefficient, 1/m
    const opacity = clamp(1 - Math.exp(-density * 0.5), 0, 0.8);
    smoke.visible = smokeVisible && opacity > 0.01;
    smoke.position.z = layer;
    smoke.scale.z = Math.max(room.ceiling_height - layer, 0.001);
    smoke.material.opacity = opacity;
    smoke.material.color.lerpColors(SMOKE_THIN, SMOKE_THICK, opacity / 0.8);
  }

  // Charring, glow and selection highlight for every object at the current fire time.
  function applyFireState() {
    for (const [id, entry] of objects) {
      const glow = entry.burn ? clamp(entry.hrr / Math.max(entry.burn.peak_kw, 1), 0, 1) : 0;
      for (const mesh of entry.meshes) {
        mesh.material.color.copy(mesh.userData.baseColor).lerp(CHAR, clamp(entry.burnt * 1.5, 0, 0.92));
        if (glow > 0) mesh.material.emissive.copy(GLOW).multiplyScalar(0.25 * glow);
        else mesh.material.emissive.copy(id === selected ? SELECTED : new THREE.Color(0));
      }
    }
  }

  function animateFlames(now) {
    let total = 0;
    const centre = new THREE.Vector3();
    for (const entry of objects.values()) {
      if (!entry.flames) continue;
      const { obj, hrr } = entry;
      const burning = hrr > 0.5;
      const active = burning ? Math.ceil(FLAMES_PER_OBJECT * clamp(hrr / 200, 0.15, 1)) : 0;
      const height = burning ? flameHeight(hrr, obj) : 0;
      const width = clamp(0.16 + 0.018 * Math.sqrt(hrr), 0.16, 0.6);
      entry.flames.forEach((sprite, i) => {
        sprite.visible = i < active;
        if (!sprite.visible) return;
        const { phase, speed, size, x, y, base } = sprite.userData;
        const life = (now * speed + phase) % 1;
        const taper = 1 - 0.6 * life;
        sprite.position.set(x * taper, y * taper, base + life * height);
        sprite.scale.set(width * size * taper, width * size * taper * 1.6, 1);
        const mix = life * 2;
        sprite.material.color.lerpColors(
          FLAME_COLORS[Math.min(Math.floor(mix), 1)], FLAME_COLORS[Math.min(Math.floor(mix), 1) + 1], mix % 1,
        );
        sprite.material.opacity = 0.85 * (1 - life) ** 1.3;
      });
      if (burning) {
        total += hrr;
        centre.addScaledVector(new THREE.Vector3(obj.position.x, obj.position.y, obj.position.z + obj.size.height + 0.3), hrr);
      }
    }
    if (total > 0) {
      fireLight.position.copy(centre.divideScalar(total));
      const flicker = 0.85 + 0.15 * Math.sin(now * 23) * Math.sin(now * 7.3);
      fireLight.intensity = clamp(Math.sqrt(total) * 0.3, 0, 14) * flicker;
    } else {
      fireLight.intensity = 0;
    }
    // The room light dims as smoke fills it.
    const dim = fire ? fire.smoke.material.opacity / 0.8 : 0;
    sky.intensity = 1.6 - 0.9 * dim;
    sun.intensity = 2.2 - 1.6 * dim;
  }

  // Floor overlay from a RiskReport: one coloured texel per cell of its heat or danger map.
  function setRisk(report, kind = 'heat') {
    if (risk) {
      root.remove(risk);
      disposeTree(risk);
      risk.children[0]?.material.map?.dispose();
      risk = null;
    }
    if (!report) return;
    const { x0, y0, cell, nx, ny, values } = report[kind];
    const colorOf = kind === 'heat' ? heatColor : harmColor;
    const pixels = new Uint8Array(nx * ny * 4);
    values.forEach((value, k) => {
      if (value === null) return;
      const hex = colorOf(value);
      pixels.set([1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).concat(190), k * 4);
    });
    const texture = new THREE.DataTexture(pixels, nx, ny, THREE.RGBAFormat);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.needsUpdate = true;
    const overlay = new THREE.Mesh(
      new THREE.PlaneGeometry(nx * cell, ny * cell),
      new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false }),
    );
    overlay.position.set(x0 + (nx * cell) / 2, y0 + (ny * cell) / 2, 0.025);
    overlay.renderOrder = 1;
    risk = new THREE.Group();
    risk.add(overlay);
    if (report.route.length > 1) {
      risk.add(ribbon(report.route.map((p) => new THREE.Vector3(p.x, p.y, 0.035)), '#ffffff'));
    }
    root.add(risk);
  }

  function setSmokeVisible(visible) {
    smokeVisible = visible;
    setTime(fireTime);
  }

  const raycaster = new THREE.Raycaster();
  let downAt = null;
  canvas.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
  canvas.addEventListener('pointerup', (e) => {
    if (!root || !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4) return;
    const rect = canvas.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
    raycaster.setFromCamera(pointer, camera);
    const meshes = [...objects.values()].flatMap((entry) => entry.meshes);
    let node = raycaster.intersectObjects(meshes, false)[0]?.object;
    while (node && !node.userData.objectId) node = node.parent;
    const id = node?.userData.objectId ?? null;
    select(id);
    onSelect?.(id);
  });

  renderer.setAnimationLoop(() => {
    if (!canvas.offsetParent) return; // not on screen
    controls.update();
    figure.update(clock.getDelta());
    animateFlames(clock.elapsedTime);
    renderer.render(scene, camera);
  });

  return { show, select, setFire, setTime, setSmokeVisible, setRisk };
}
