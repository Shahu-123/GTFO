# Contributing

Thanks for taking an interest. GTFO is a small project and a simplified model, so the
most valuable contributions are ones that make it more honest or more accurate.

## Setting up

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

`ffmpeg` must be installed. The OpenAI key is only needed for the video step; everything
else runs against the sample room.

## Where help is welcome

- **Fire science review.** The fuel table (`firesim/fire/fuels.py`) and the human
  behaviour constants (top of `firesim/escape/sim.py`) are literature values and stated
  assumptions. Corrections with a source are very welcome.
- **Validation.** `tests/test_fire.py` checks the smoke layer against the MQH
  correlation. More comparisons against published experiments or CFAST would be valuable.
- **Multi-room support.** The biggest missing piece. See the roadmap in the README.
- **Capture quality.** Smarter frame selection from the uploaded video.

## Ground rules

- The language model describes the room. It must never decide a safety result; the
  simulation stays deterministic.
- Keep assumptions as named constants with a comment saying where the number comes from.
- Run `ruff format . && ruff check .` and `pytest` before opening a pull request.
- `CLAUDE.md` describes the architecture in detail; keep it current when you change the
  structure.
