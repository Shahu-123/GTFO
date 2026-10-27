import { HARM_STOPS, HEAT_STOPS, gradient } from './colors.js';
import { drawPlan } from './plan.js';
import { createViewer } from './viewer.js';

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
const viewer = createViewer($('view'), markSelected);
// A second, independent 3D view of the same room for the risk maps.
const riskViewer = createViewer($('risk-canvas'), markSelected);

let current = null; // { id, room }
let run = null; // FireRun being played back
let escape = null; // EscapeRun for the same scenario
let risk = null; // RiskReport for the current room
let playing = false;
let time = 0;
let lastFrame = 0;

function setStatus(text, isError = false, target = 'status') {
  $(target).textContent = text;
  $(target).classList.toggle('error', isError);
}

function fillList(list, items) {
  list.replaceChildren(...items.map((text) => el('li', { textContent: text })));
}

function markSelected(id) {
  for (const li of $('objects').children) li.classList.toggle('selected', li.dataset.id === id);
}

function floorStats(floor) {
  const xs = floor.map((p) => p.x);
  const ys = floor.map((p) => p.y);
  const area = Math.abs(floor.reduce((sum, p, i) => {
    const q = floor[(i + 1) % floor.length];
    return sum + (p.x * q.y - q.x * p.y) / 2;
  }, 0));
  return { width: Math.max(...xs) - Math.min(...xs), depth: Math.max(...ys) - Math.min(...ys), area };
}

function showRoom({ id, room, problems }) {
  current = { id, room };
  clearFire();
  risk = null;
  $('risk-results').hidden = true;
  $('tabs').hidden = true;
  setTab('sim');
  setStatus('', false, 'risk-status');
  viewer.show(room);

  const { width, depth, area } = floorStats(room.floor);
  $('room-name').textContent = room.name;
  $('room-dims').textContent =
    `${width.toFixed(2)} × ${depth.toFixed(2)} m, ceiling ${room.ceiling_height.toFixed(2)} m, ` +
    `${area.toFixed(1)} m². Confidence: ${room.confidence}.`;
  fillList($('problems'), problems);
  fillList($('scale'), room.scale_references);
  fillList($('assumptions'), room.assumptions);

  $('objects').replaceChildren(...room.objects.map((obj) => {
    const li = el('li');
    li.dataset.id = obj.id;
    const meta = el('span', { className: 'meta', textContent: obj.fuel.replaceAll('_', ' ') });
    if (obj.is_ignition_source) {
      meta.textContent += ' · ignition';
      meta.classList.add('ignition');
    }
    li.append(el('span', { textContent: obj.label }), meta);
    li.addEventListener('click', () => {
      viewer.select(obj.id);
      markSelected(obj.id);
    });
    return li;
  }));

  // Likely ignition sources first, then everything else that can burn.
  const burnable = room.objects.filter((obj) => obj.fuel !== 'non_combustible');
  const ordered = [...burnable.filter((o) => o.is_ignition_source), ...burnable.filter((o) => !o.is_ignition_source)];
  $('ignition').replaceChildren(...ordered.map((obj) => el('option', { value: obj.id, textContent: obj.label })));
  $('openings').replaceChildren(...room.openings
    .filter((o) => o.kind !== 'open_passage')
    .map((o) => {
      const box = el('input', { type: 'checkbox', checked: o.is_open });
      box.dataset.id = o.id;
      const label = el('label', { className: 'toggle' });
      label.append(box, ` ${o.kind === 'door' ? 'Door' : 'Window'} open (${o.id.replaceAll('_', ' ')})`);
      return label;
    }));
  $('run-fire').disabled = ordered.length === 0;

  // Places a person could be: on a bed or a seat, or standing in the middle of the room.
  const restingPlaces = room.objects.filter(
    (obj) => RESTING.test(obj.category) && obj.position.z < 0.9,
  );
  $('person-at').replaceChildren(
    ...restingPlaces.map((obj) => el('option', { value: obj.id, textContent: `on the ${obj.label}` })),
    el('option', { value: '', textContent: 'standing in the middle of the room' }),
  );
  syncAsleep();

  $('details').hidden = false;
  history.replaceState(null, '', `#${id}`);
}

const RESTING = /\b(bed|mattress|sofa|couch|chair|seat)\b/i;
const SLEEPING = /\b(bed|mattress|sofa|couch)\b/i;

// Default to asleep when the chosen place is somewhere people sleep.
function syncAsleep() {
  const obj = current.room.objects.find((o) => o.id === $('person-at').value);
  $('asleep').checked = Boolean(obj && SLEEPING.test(obj.category));
}

function personScenario(room) {
  const obj = room.objects.find((o) => o.id === $('person-at').value);
  const xs = room.floor.map((p) => p.x);
  const ys = room.floor.map((p) => p.y);
  const middle = { x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 };
  return {
    start: obj ? { x: obj.position.x, y: obj.position.y } : middle,
    start_object_id: obj?.id ?? null,
    asleep: $('asleep').checked,
    smoke_alarm: $('alarm').checked,
    stays_low: $('stays-low').checked,
    knows_room: $('knows-room').checked,
  };
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : null;
    throw new Error(detail ?? `Request failed (${response.status})`);
  }
  return body;
}

async function loadRoom(id) {
  setStatus('Loading…');
  try {
    showRoom(await fetchJson(`/api/rooms/${encodeURIComponent(id)}`));
    setStatus('');
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function refreshSaved() {
  const ids = await fetchJson('/api/rooms').catch(() => []);
  const options = ids.map((id) => el('option', { value: id, textContent: id }));
  $('saved').replaceChildren($('saved').firstElementChild, ...options);
}

// ---- Fire playback ----

const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const at = (series, t) => series[Math.min(Math.round(t), series.length - 1)];

function clearFire() {
  run = null;
  escape = null;
  setPlaying(false);
  for (const id of ['fire-results', 'readout', 'timeline']) $(id).hidden = true;
  setStatus('', false, 'fire-status');
}

function setPlaying(value) {
  playing = value;
  $('play').textContent = playing ? 'Pause' : 'Play';
  if (playing) {
    lastFrame = performance.now();
    requestAnimationFrame(tick);
  }
}

function seek(t) {
  if (!run) return;
  time = Math.min(Math.max(t, 0), run.t.at(-1));
  viewer.setTime(time);
  $('scrub').value = time;
  $('r-time').textContent = clock(time);
  $('r-hrr').textContent = `${Math.round(at(run.hrr_kw, time)).toLocaleString()} kW`;
  $('r-layer').textContent = `${at(run.layer_height_m, time).toFixed(2)} m above floor`;
  $('r-temp').textContent = `${Math.round(at(run.upper_temp_c, time))} °C`;
  $('r-vis').textContent = `${at(run.visibility_m, time).toFixed(1)} m`;
  $('r-co').textContent = `${at(run.co_ppm, time).toLocaleString()} ppm`;
  const i = Math.min(Math.round(time / escape.dt), escape.x.length - 1);
  $('r-person').textContent = personStatus(time, escape.posture[i]);
  $('r-heat').textContent = escape.fed_heat[i].toFixed(2);
  $('r-gas').textContent = escape.fed_gas[i].toFixed(2);
  for (const li of $('events').children) li.classList.toggle('past', Number(li.dataset.t) <= time);
}

function personStatus(t, posture) {
  const s = escape.summary;
  if (s.escaped_s !== null && t >= s.escaped_s) return 'out of the room';
  if (posture === 'collapsed') return 'collapsed';
  if (s.aware_s === null || t < s.aware_s) return posture === 'lying' ? 'asleep' : 'unaware of the fire';
  if (s.moving_s === null || t < s.moving_s) return s.outcome === 'trapped' ? 'trapped' : 'reacting';
  return { upright: 'walking', crouch: 'crouching under smoke', crawl: 'crawling under smoke' }[posture] ?? posture;
}

// One-line verdict and how serious it is.
function outcome(s) {
  const dose = Math.max(s.fed_gas, s.fed_heat);
  if (s.outcome === 'escaped' && dose < 0.3) return [`Escaped in ${clock(s.escaped_s)}, unharmed.`, ''];
  if (s.outcome === 'escaped') {
    return [`Escaped in ${clock(s.escaped_s)}, but took a dose of ${dose.toFixed(2)}: enough to overcome a child or an older person.`, 'warn'];
  }
  if (s.outcome === 'incapacitated') return [`Overcome by ${s.incapacitated_by} at ${clock(s.incapacitated_s)}. Did not get out.`, 'bad'];
  if (s.outcome === 'trapped') return ['Trapped: no clear route to the door.', 'bad'];
  if (s.outcome === 'unaware') return ['Never became aware of the fire.', 'warn'];
  return ['Still in the room when the simulation ended.', 'warn'];
}

function tick(now) {
  if (!playing || !run) return;
  seek(time + ((now - lastFrame) / 1000) * Number($('speed').value));
  lastFrame = now;
  if (time >= run.t.at(-1)) setPlaying(false);
  else requestAnimationFrame(tick);
}

function showFire(result, room) {
  run = result.fire;
  escape = result.escape;
  viewer.show(room, { keepCamera: true });
  viewer.setFire(run, escape);
  viewer.setSmokeVisible($('smoke-toggle').checked);

  const s = run.summary;
  const e = escape.summary;
  const when = (seconds) => (seconds === null ? 'not reached' : clock(seconds));
  const [verdict, severity] = outcome(e);
  $('outcome').textContent = verdict;
  $('outcome').className = severity;
  const rows = [
    ['Smoke alarm sounds', e.alarm_s === null ? 'no alarm' : clock(e.alarm_s)],
    ['Person aware of fire', when(e.aware_s)],
    ['Starts moving', when(e.moving_s)],
    ['Out of the room', when(e.escaped_s)],
    ['Smoke below head height', when(s.smoke_at_head_height_s)],
    ['Visibility under 5 m', when(s.visibility_below_5m_s)],
    ['Smoke layer over 200 °C', when(s.layer_above_200c_s)],
    ['Flashover', when(s.flashover_s)],
    ['Peak fire size', `${Math.round(s.peak_hrr_kw).toLocaleString()} kW`],
    ['Items burned', `${s.items_burned} of ${room.objects.length}`],
  ];
  $('fire-summary').replaceChildren(...rows.flatMap(([term, value]) => [
    el('dt', { textContent: term }), el('dd', { textContent: value }),
  ]));

  const events = [...run.events, ...escape.events].sort((a, b) => a.t - b.t);
  $('events').replaceChildren(...events.map((event) => {
    const li = el('li', { className: event.kind });
    li.dataset.t = event.t;
    li.append(el('time', { textContent: clock(event.t) }), el('span', { textContent: event.text }));
    li.addEventListener('click', () => {
      seek(event.t);
      if (event.object_id) {
        viewer.select(event.object_id);
        markSelected(event.object_id);
      }
    });
    return li;
  }));

  $('scrub').max = run.t.at(-1);
  for (const id of ['fire-results', 'readout', 'timeline']) $(id).hidden = false;
  seek(0);
  setPlaying(true);
}

$('scenario').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!current) return;
  const states = Object.fromEntries(
    [...$('openings').querySelectorAll('input')].map((box) => [box.dataset.id, box.checked]),
  );
  const scenario = {
    fire: { ignition_object_id: $('ignition').value, opening_states: states },
    person: personScenario(current.room),
  };
  // Draw the room with the doors and windows as chosen for this scenario.
  const room = structuredClone(current.room);
  for (const opening of room.openings) {
    if (opening.id in states) opening.is_open = states[opening.id];
  }
  $('run-fire').disabled = true;
  setStatus('Simulating…', false, 'fire-status');
  try {
    const result = await fetchJson(`/api/rooms/${encodeURIComponent(current.id)}/escape`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scenario),
    });
    setStatus('', false, 'fire-status');
    showFire(result, room);
  } catch (err) {
    setStatus(err.message, true, 'fire-status');
  } finally {
    $('run-fire').disabled = false;
  }
});

$('play').addEventListener('click', () => {
  if (!run) return;
  if (!playing && time >= run.t.at(-1)) seek(0);
  setPlaying(!playing);
});
$('scrub').addEventListener('input', (event) => {
  setPlaying(false);
  seek(Number(event.target.value));
});
$('smoke-toggle').addEventListener('change', (event) => viewer.setSmokeVisible(event.target.checked));
$('person-at').addEventListener('change', syncAsleep);

// ---- Risk map ----

const PERSON_TEXT = {
  asleep_alarm: 'asleep, with a smoke alarm',
  asleep_no_alarm: 'asleep, no smoke alarm',
  awake: 'awake',
};

function setTab(name) {
  $('sim-view').hidden = name !== 'sim';
  $('risk-view').hidden = name !== 'risk';
  $('tab-sim').classList.toggle('active', name === 'sim');
  $('tab-risk').classList.toggle('active', name === 'risk');
}

const overlayKind = () => document.querySelector('input[name="overlay"]:checked').value;

function caseText(c) {
  if (c.worst_outcome === 'incapacitated') return `${PERSON_TEXT[c.person]}: overcome before getting out`;
  if (c.worst_outcome === 'trapped') return `${PERSON_TEXT[c.person]}: cannot reach the door`;
  if (c.escape_s === null) return `${PERSON_TEXT[c.person]}: never aware (fire burns out)`;
  return `${PERSON_TEXT[c.person]}: out in ${clock(c.escape_s)}, dose ${c.max_dose.toFixed(2)}`;
}

// Fill the scenario form from a risk case and run it, so the user can watch it.
function replay(source, c) {
  $('ignition').value = source.object_id;
  $('person-at').value = c.scenario.start_object_id ?? '';
  $('asleep').checked = c.scenario.asleep;
  $('alarm').checked = c.scenario.smoke_alarm;
  $('stays-low').checked = c.scenario.stays_low;
  $('knows-room').checked = c.scenario.knows_room;
  for (const box of $('openings').querySelectorAll('input')) {
    if (box.dataset.id in c.opening_states) box.checked = c.opening_states[box.dataset.id];
    else box.checked = current.room.openings.find((o) => o.id === box.dataset.id).is_open;
  }
  setTab('sim');
  $('scenario').requestSubmit();
}

const level = (value) => (value >= 0.6 ? 'high' : value >= 0.3 ? 'medium' : '');

function showRisk(report) {
  risk = report;
  $('tabs').hidden = false;
  setTab('risk');
  riskViewer.show(current.room);
  riskViewer.setRisk(risk, overlayKind());
  drawPlan($('plan-heat'), current.room, risk, 'heat');
  drawPlan($('plan-danger'), current.room, risk, 'danger');
  const fires = risk.sources.length;
  $('note-heat').textContent =
    `Colour shows the soonest each spot becomes too hot for a person to stay there, taking the ` +
    `earliest of the ${fires} fires tried (one per likely ignition source). Red areas become ` +
    `dangerous first. They surround the items most likely to start or feed a fire.`;
  $('note-danger').textContent =
    `Colour shows the average harm to someone awake who is at that spot when a fire starts: ` +
    `averaged over the same ${fires} fires and over people who start leaving ` +
    `${new Intl.ListFormat('en-GB').format(risk.danger_delays_s.map(String))} seconds after noticing. The bed shows the average for ` +
    `someone asleep there, with and without a smoke alarm.`;
  $('risk-headline').textContent =
    `${report.harmful_cases} of ${report.total_cases} simulated cases end with the person harmed ` +
    `(${report.fires} fires, ${report.runs} escape runs).`;

  $('factors').replaceChildren(...report.factors.map((factor) => {
    const li = el('li', { className: level(factor.severity) });
    li.append(el('strong', { textContent: factor.title }), el('span', { textContent: factor.detail }));
    li.addEventListener('click', () => {
      viewer.select(factor.object_ids[0] ?? null);
      markSelected(factor.object_ids[0] ?? null);
    });
    return li;
  }));

  $('sources').replaceChildren(...report.sources.map((source) => {
    const worst = source.cases.reduce((a, b) => (b.harm > a.harm ? b : a));
    const li = el('li', { className: level(source.harm * 2), title: 'Click to play the worst case' });
    li.append(el('strong', { textContent: source.label }), el('span', { textContent: caseText(worst) }));
    li.addEventListener('click', () => replay(source, worst));
    return li;
  }));
  $('risk-results').hidden = false;
}

$('run-risk').addEventListener('click', async () => {
  if (!current) return;
  $('run-risk').disabled = true;
  setStatus('Running every likely fire against a sleeper and someone awake…', false, 'risk-status');
  try {
    showRisk(await fetchJson(`/api/rooms/${encodeURIComponent(current.id)}/risk`, { method: 'POST' }));
    setStatus('', false, 'risk-status');
  } catch (err) {
    setStatus(err.message, true, 'risk-status');
  } finally {
    $('run-risk').disabled = false;
  }
});
$('tab-sim').addEventListener('click', () => setTab('sim'));
$('tab-risk').addEventListener('click', () => setTab('risk'));
for (const input of document.querySelectorAll('input[name="overlay"]')) {
  input.addEventListener('change', () => risk && riskViewer.setRisk(risk, overlayKind()));
}
// Colour bar with tick labels placed where their values fall on the scale.
function legend(name, stops, ticks) {
  const [min, max] = [stops[0][0], stops.at(-1)[0]];
  $(`legend-${name}`).style.background = gradient(stops);
  $(`ticks-${name}`).replaceChildren(...ticks.map(([value, text]) => {
    const span = el('span', { textContent: text });
    const at = ((value - min) / (max - min)) * 100;
    span.style.left = `${at}%`;
    // End labels hug the ends; middle labels start at their value so they cannot overlap the left end.
    span.style.transform = `translateX(${at === 100 ? '-100%' : at === 0 ? '0' : '-10%'})`;
    return span;
  }));
}
legend('heat', HEAT_STOPS, [[0, 'at once'], [60, '1 min'], [120, '2 min'], [240, '4 min or more']]);
legend('danger', HARM_STOPS, [[0, '0 unharmed'], [0.3, '0.3 could overcome a child or older person'], [1, '1 does not get out']]);

// ---- Room loading ----

$('upload').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('submit').disabled = true;
  setStatus('Sampling frames and building the room. This can take a few minutes…');
  try {
    showRoom(await fetchJson('/api/rooms', { method: 'POST', body: new FormData(event.target) }));
    setStatus('Done. Check the dimensions before trusting the layout.');
    refreshSaved();
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $('submit').disabled = false;
  }
});

$('sample').addEventListener('click', () => loadRoom('sample'));
$('saved').addEventListener('change', (event) => event.target.value && loadRoom(event.target.value));

refreshSaved();
loadRoom(location.hash.slice(1) || 'sample');
