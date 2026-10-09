// A jointed human figure animated in code, so it can take every posture the escape
// simulation produces. Built in room coordinates: z up, +x is the way the person faces.
// Angles are rotations about the left-right (y) axis: positive swings a hanging limb back.

import * as THREE from 'three';

const HIP_WIDTH = 0.09;
const SHOULDER_WIDTH = 0.19;
const THIGH = 0.45;
const SHIN = 0.45;
const TORSO = 0.55;
const UPPER_ARM = 0.3;
const FOREARM = 0.28;

// Joint angles (radians) and body placement for each posture. swing is how far the limbs
// move with each step.
const POSES = {
  upright: { bodyPitch: 0, bodyX: 0, lift: 0, hip: 0.9, torso: 0.06, head: 0, thigh: 0, knee: 0.1, shoulder: 0, elbow: -0.3, swing: 0.5 },
  crouch: { bodyPitch: 0, bodyX: 0, lift: 0, hip: 0.66, torso: 0.75, head: -0.5, thigh: -0.9, knee: 1.5, shoulder: -0.5, elbow: -0.8, swing: 0.35 },
  crawl: { bodyPitch: 0, bodyX: 0, lift: 0, hip: 0.45, torso: 1.45, head: -1.1, thigh: -0.3, knee: 1.87, shoulder: -1.45, elbow: -0.15, swing: 0.25 },
  lying: { bodyPitch: -Math.PI / 2, bodyX: 0.05, lift: 0.12, hip: 0, torso: 0, head: 0, thigh: 0, knee: 0, shoulder: 0, elbow: 0, swing: 0 },
  collapsed: { bodyPitch: Math.PI / 2, bodyX: -0.3, lift: 0.12, hip: 0, torso: 0, head: 0.3, thigh: 0.1, knee: 0.5, shoulder: -2.6, elbow: -0.6, swing: 0 },
};

function material(color) {
  // Marked transparent so the figure is drawn after the smoke and stays visible in it.
  return new THREE.MeshStandardMaterial({ color, roughness: 0.7, transparent: true });
}

// Capsule that hangs from its pivot (direction -1) or stands on it (direction +1).
function segment(length, radius, color, direction = -1) {
  const geometry = new THREE.CapsuleGeometry(radius, Math.max(length - 2 * radius, 0.01), 4, 10);
  geometry.rotateX(Math.PI / 2);
  geometry.translate(0, 0, (direction * length) / 2);
  const mesh = new THREE.Mesh(geometry, material(color));
  mesh.castShadow = true;
  mesh.renderOrder = 12;
  return mesh;
}

function joint(parent, x, y, z) {
  const group = new THREE.Group();
  group.position.set(x, y, z);
  parent.add(group);
  return group;
}

export function createFigure() {
  const skin = '#e8b88a';
  const top = '#2f80ed';
  const trousers = '#26364d';

  const group = new THREE.Group();
  const body = joint(group, 0, 0, 0);
  const hips = joint(body, 0, 0, 0);
  hips.add(segment(0.2, 0.12, trousers, 1).translateZ(-0.08));

  const torso = joint(hips, 0, 0, 0);
  torso.add(segment(TORSO, 0.14, top, 1));
  const head = joint(torso, 0, 0, TORSO + 0.03);
  const skull = new THREE.Mesh(new THREE.SphereGeometry(0.11, 20, 14), material(skin));
  skull.position.z = 0.1;
  skull.castShadow = true;
  skull.renderOrder = 12;
  head.add(skull);

  const limbs = {};
  for (const [side, sign] of [['L', 1], ['R', -1]]) {
    const shoulder = joint(torso, 0, sign * SHOULDER_WIDTH, TORSO - 0.06);
    shoulder.add(segment(UPPER_ARM, 0.045, top));
    const elbow = joint(shoulder, 0, 0, -UPPER_ARM);
    elbow.add(segment(FOREARM, 0.04, skin));

    const thigh = joint(hips, 0, sign * HIP_WIDTH, 0);
    thigh.add(segment(THIGH, 0.075, trousers));
    const knee = joint(thigh, 0, 0, -THIGH);
    knee.add(segment(SHIN, 0.055, trousers));
    limbs[side] = { shoulder, elbow, thigh, knee };
  }

  const current = { ...POSES.upright };
  let target = POSES.upright;
  let phase = 0;
  let stepping = false;

  function setPose(posture, gaitPhase, moving) {
    target = POSES[posture] ?? POSES.upright;
    phase = gaitPhase;
    stepping = moving;
  }

  function update(dt) {
    const ease = 1 - Math.exp(-dt * 9);
    for (const key of Object.keys(current)) current[key] += (target[key] - current[key]) * ease;

    const stride = stepping ? current.swing : 0;
    const swing = Math.sin(phase) * stride;
    body.rotation.y = current.bodyPitch;
    body.position.set(current.bodyX, 0, current.lift);
    hips.position.z = current.hip + (stepping ? 0.02 * Math.abs(Math.cos(phase)) : 0);
    torso.rotation.y = current.torso;
    head.rotation.y = current.head;

    limbs.L.thigh.rotation.y = current.thigh - swing;
    limbs.R.thigh.rotation.y = current.thigh + swing;
    limbs.L.knee.rotation.y = current.knee + stride * 1.2 * Math.max(0, Math.cos(phase));
    limbs.R.knee.rotation.y = current.knee + stride * 1.2 * Math.max(0, -Math.cos(phase));
    limbs.L.shoulder.rotation.y = current.shoulder + swing * 0.8;
    limbs.R.shoulder.rotation.y = current.shoulder - swing * 0.8;
    limbs.L.elbow.rotation.y = current.elbow;
    limbs.R.elbow.rotation.y = current.elbow;
  }

  update(1);
  return { group, setPose, update };
}
