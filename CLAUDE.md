# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GTFO (Get the fire out, Python package `firesim`) turns a phone walkthrough video of a room into a 3D scene, simulates a fire in it
and a person escaping, and shows which areas and items make the room dangerous. All four
stages are built for a single room: video to 3D room, fire simulation, one occupant
escaping, and a risk analysis over many fires and occupants, with playback in the browser.

## Commands

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first-time setup
.venv/bin/uvicorn firesim.app:app --reload --port 8000          # run, then open http://localhost:8000
.venv/bin/pytest                                                # all tests
.venv/bin/pytest tests/test_schema.py::test_missing_exit_is_reported   # one test
.venv/bin/ruff format . && .venv/bin/ruff check . --fix         # format and lint
```

`ffmpeg` and `ffprobe` must be on PATH. `OPENAI_API_KEY` goes in `.env` (see `.env.example`);
settings are re-read per request, so no restart is needed after editing `.env`.

The Claude desktop preview launcher cannot execute `.venv` under `~/Desktop` (macOS
permission error on `pyvenv.cfg`). Start uvicorn from a shell; `.claude/launch.json` only
attaches the preview to `http://localhost:8000`.

## Architecture

The `Room` model in `firesim/schema.py` is the contract for everything:

- It is the structured-output schema GPT-6.1 fills in (`firesim/reconstruct.py`).
- It is the JSON the Three.js viewer draws (`web/viewer.js`).
- It is the input to the fire simulation (`firesim/fire/`) and the escape simulation
  (`firesim/escape/`).

Because the same Pydantic model is sent to the API as a strict JSON schema, it must not use
numeric constraints, tuples or defaults. Plausibility checks live in `check_room`, which
returns a list of problems that the UI shows instead of rejecting the room. The model
estimates dimensions from images, so its output is never treated as measured.

Pipeline for `POST /api/rooms`: save upload to `data/rooms/<id>/` → `frames.py` samples
evenly spaced JPEGs with ffmpeg (GPT-6.1 takes images, not video) → `reconstruct.py` sends
the frames and any user-supplied measurements through `client.responses.parse` → `room.json`
is saved and returned with `check_room` problems. `GET /api/rooms/sample` serves
`tests/fixtures/sample_room.json`, which lets the viewer be worked on without an API key.

Coordinate convention: metres, right-handed, z up, floor at z = 0. Floor corners are listed
counter-clockwise; wall i runs from corner i to corner i + 1. Opening `offset` is measured
along the wall from its start corner. Object `position` is the footprint centre with z as
the base height; `parts` are primitives in the object's local frame. The viewer builds the
scene in these coordinates inside a root group rotated into three.js's y-up frame, so new
geometry code should stay in room coordinates.

### Fire simulation (`firesim/fire/`)

`POST /api/rooms/<id>/fire` takes a `FireScenario` (where the fire starts, which openings
are open) and returns a `FireRun`: once-per-second series (fire size, smoke layer height,
smoke temperature, visibility, CO, CO2, O2), per-object heat release, and a list of events
with causes. The viewer plays this back; nothing is simulated in the browser.

- `fuels.py`: per-`FuelClass` property table and the t² burn curve of one item. The values
  are literature order-of-magnitude figures and are the main calibration knobs.
- `physics.py`: stateless relations (McCaffrey plume, vent flow, point-source radiation,
  view factor to the smoke layer).
- `zone.py`: `SmokeLayer`, the two-zone state. It tracks the layer's volume and mass at
  constant pressure (temperature follows from the two) and solves the floor pressure each
  step so that net vent outflow equals gas expansion.
- `sim.py`: `run_fire` ties them together: oxygen-limited burning, spread by radiant flux
  against the 10/20/40 kW/m² ignition classes, window breakage, flashover, events.

`tests/test_fire.py` checks the layer temperature against the MQH correlation; keep that
test passing when changing `zone.py`, since it is the only external validation. The door
and windows lead to ambient air: the rest of the home is not modelled.

### Escape simulation (`firesim/escape/`)

`POST /api/rooms/<id>/escape` takes a fire scenario and a `PersonScenario`, runs the fire,
then runs one occupant through it and returns both timelines. The fire does not react to
the person (a door they open is not reopened in the fire model).

- `grid.py`: 0.1 m walkable grid (furniture inflated by body radius, low clutter slows
  instead of blocking) and Dijkstra from the exit cells.
- `dose.py`: FED rates for toxic gas and heat (ISO 13571 / Purser). No HCN or irritants.
- `sim.py`: `run_escape` is a state machine: unaware, reacting, moving, at_door, then
  escaped, collapsed or trapped. Detection cues, reaction delays and speeds are constants
  at the top of the file and are assumptions, so change them there, not inline. The cost
  of reaching an exit from every cell is recomputed each second with cells near fires made
  expensive, and the person steers down that field. Only doors and open passages count as
  exits; windows never do.
- A sleeper with no alarm is woken by the fire's own signals: sound, flickering light and
  smell, each with a probability and delay taken from Bruck and Brennan (2001) and drawn
  from the seed, then by smoke in the face or painful heat as a last resort. The fire size
  at which sound and light begin is an assumption. The figures are for unimpaired adults;
  children, older people and anyone affected by alcohol or sleeping tablets are not modelled.
- With their head in smoke thinner than `SEE_RANGE` of visibility, the person's heading
  wanders at random, they walk into furniture and slide along it. This is seeded
  (`PersonScenario.seed`), so a run is repeatable but different seeds differ; anything that
  reports a single number for a smoky scenario should average over seeds.

The person's series are sampled every `dt` (0.25 s), unlike the fire's 1 s series. While
the person is not moving the loop steps a full second and repeats the sample, which is
what keeps the risk analysis fast.

### Risk analysis (`firesim/risk.py`)

`POST /api/rooms/<id>/risk` runs `analyse`. For each object flagged `is_ignition_source`,
`_analyse_source` runs the fire with the doors as filmed and again with them the other way,
sends three occupants through (sleeper with alarm, sleeper without, someone awake; three
seeds each), and places an awake person at a lattice of floor spots. The API runs one
source per worker process (`parallel=True`); results are identical to the serial path, and
it falls back to serial if the pool cannot start. The `RiskReport` holds:

- `heat` (shown as the "Risk map"): per 0.2 m floor cell, the soonest that cell passes
  2.5 kW/m² of heat in any of the fires. The earliest time is used because the mean is
  nearly flat in a small room.
- `danger` (shown as the "Danger map"): per cell, the mean harm to an awake person who is
  there when a fire starts, over all fires and over the reaction delays in `SPOT_DELAYS`.
  People are simulated at every second cell and the rest is interpolated.
- `resting`: beds and sofas with the mean harm to a sleeper there.
- `sources`: ignition sources ranked by mean harm, each with replayable cases.
- `factors`: what makes the room risky, from comparing runs (smoke alarm, door state) and
  from the layout (ignition source beside the way out, fast spread, clutter, narrow route).
- `main_items` and `high_risk_items`: the object ids a plan should draw.

Harm per run is 1 for incapacitated or trapped, otherwise the larger FED dose.

### Viewer

`web/viewer.js` draws the room and plays back both timelines; `web/figure.js` is a jointed
figure animated in code, with one pose table per posture (upright, crouch, crawl, lying,
collapsed). `web/app.js` owns the forms, the playback clock and the readouts. Anything that
must stay visible inside smoke (flames, figure, route) is marked transparent with a
`renderOrder` above the smoke slab's.

The stage has two tabs. "Simulation" is the playback viewer. "Risk map" holds a second,
independent `createViewer` instance with the chosen map on its floor, and two SVG plans
drawn by `web/plan.js` (one per map), showing only `main_items` and `high_risk_items`,
labelled by object category. `web/colors.js` holds the two colour scales, shared by the
3D overlay, the plans and the legends. A viewer skips rendering while its tab is hidden.

## Decisions already made

- GPT-6.1 Sol (`gpt-6.1-sol`, Responses API) produces the 3D scene from the user's video.
  It outputs text only, so it describes the scene as data and the viewer renders it.
- LLM output never decides safety results. The simulation is deterministic.
- The fire engine is our own two-zone model in Python, not NIST CFAST. CFAST was the first
  choice, but it needs a Fortran compiler to build on macOS and none is installed
  (`brew install gcc` would provide one). CFAST via PyCFAST remains the intended
  cross-check for `zone.py`.
- The human figure is procedural (`web/figure.js`), not a Mixamo model. Mixamo downloads
  need the user's Adobe login, and no stock clip set covers crawling, lying and collapsing.
  Swapping in a rigged GLB later is a viewer-only change.
- An item's peak heat release is capped by its fuel (`MIN_BURN_TIME` in `fuels.py`), because
  bounding boxes of thin or sparse items (rugs, cables, curtains) overstate what can burn.
- Not built yet: more than one room, more than one occupant, start positions other than
  the bed and a seat, and counterfactuals that change the layout (moving or removing an
  item).
- Results are educational, not a validated fire safety assessment, and the UI must say so.
