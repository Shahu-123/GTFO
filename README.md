# Firesim

Film a room with your phone, get a 3D model of it, then watch how a fire would spread and
how a person would get out. Firesim shows which items and areas make the room dangerous
and why.

This is a working prototype for a single room, aimed at becoming an iOS app for whole
homes. It is an educational estimate, not a fire safety assessment.

## What it does

1. **Video to 3D room.** Frames from a walkthrough video go to GPT-6.1, which returns the
   room as data: floor outline, doors and windows, and every piece of furniture with its
   size, position, material and whether it could start a fire. The browser draws it.
2. **Fire simulation.** A two-zone fire model (hot smoke layer over clean air) with
   t² burning items, spread by radiant heat, oxygen-limited burning, window breakage and
   flashover. Validated against the MQH temperature correlation.
3. **Escape simulation.** One occupant who notices the fire (alarm, fire noise, light,
   smell, smoke or heat), reacts, and makes for a door around the furniture, crouching
   and crawling under smoke, losing their way when they cannot see, and accumulating a
   heat and toxic gas dose on the way.
4. **Risk analysis.** Every likely ignition source is tried against a sleeper with and
   without a smoke alarm and someone awake. The result is a ranked list of risk factors,
   a map of where the floor becomes too hot soonest, and a map of how a person at each
   spot would fare.

Everything after step 1 is deterministic. The language model never decides a safety
result.

## Running it

Requirements: Python 3.11 or newer, `ffmpeg` and `ffprobe` on the PATH, and an OpenAI
API key for the video step. The viewer and the simulations work without a key using the
built-in sample room.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env            # then put your OPENAI_API_KEY in .env
.venv/bin/uvicorn firesim.app:app --reload --port 8000
```

Open http://localhost:8000, click **Load sample** or upload a video, choose where the fire
starts and where the person is, and click **Run simulation**. **Build risk map** runs the
full analysis and opens the Risk map tab.

Each room reconstruction costs roughly US$0.20 in API usage and takes a few minutes. The
simulations run locally in under a second; the risk analysis takes a few seconds.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff format . && .venv/bin/ruff check .
```

## Layout

| Path | What it holds |
|---|---|
| `firesim/schema.py` | The `Room` model shared by every stage |
| `firesim/frames.py`, `firesim/reconstruct.py` | Video frames and the GPT-6.1 call |
| `firesim/fire/` | Fuel table, physics, smoke layer and the fire run |
| `firesim/escape/` | Walkable grid, dose model and the occupant |
| `firesim/risk.py` | Multi-scenario analysis, maps and risk factors |
| `firesim/app.py` | FastAPI routes |
| `web/` | Three.js viewer, animated figure, 2D plans |
| `tests/` | Pytest suite, with `fixtures/sample_room.json` |

`CLAUDE.md` describes the architecture and the decisions behind it in more detail.

## Limits

- One room only. Doors and windows open onto fresh air; the rest of the home is not
  modelled yet.
- Room dimensions are estimated from images, not measured. Known measurements can be
  typed in and improve the result.
- Fuel properties and human behaviour are literature values and stated assumptions, set
  as named constants at the top of the relevant files.
- Hydrogen cyanide, irritant gases and smouldering fires are not modelled, so some
  outcomes are optimistic.

## Privacy

Video frames are sent to OpenAI to build the room. Uploaded videos, frames and
reconstructed rooms are stored under `data/`, which is not committed.
