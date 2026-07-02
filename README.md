# J.A.R.V.I.S. — Personal AI Assistant

A personal assistant built on the Claude API: persistent memory, a coding-project
assistant, scheduled/proactive checks (including school deadline tracking), and
eventually voice.

Full plan and reasoning: see `docs/personal_jarvis_plan.md`.

## Status
Environment setup complete: repo on GitHub, Python venv working, API key verified (`src/test_connection.py` passes).
Next: build the phase 1 CLI assistant (prompt in, memory loaded, Claude called, memory written back).

## Setup
1. Python 3.11+, in WSL2 (Ubuntu) on Windows.
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and add your Anthropic API key.

## Structure
- `docs/` — planning docs
- `src/` — assistant code
- `memory/` — local persistent memory (SQLite/markdown), gitignored — this is your personal data, not tracked in git
