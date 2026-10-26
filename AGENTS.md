# Repository Guidelines

## Project Structure & Module Organization
- `custom_components/frigate_feed/`: Home Assistant custom integration backend.
  - `__init__.py`: setup/unload, WebSocket commands, card static path + Lovelace resource.
  - `frigate_source.py`: read-only adapter over the official Frigate integration (`hass.data["frigate"]`).
  - `feed.py`: Frigate review item (+ its events) -> feed item mapping, list cache, hidden items.
  - `media.py`: authenticated snapshot/review clip/thumbnail proxy + URL signing.
  - `mqtt_listener.py`: `frigate/reviews` -> debounced live-update signal.
  - `models.py`, `storage.py`, `const.py`: data model, persistence, constants.
  - `frontend/frigate-feed-card.js`: bundled Lovelace card served by the integration.
  - `manifest.json`, `strings.json`, `translations/`, `config_flow.py`: HA integration metadata/UI.
- `tests/`: pytest suite (`pytest-homeassistant-custom-component`), shared fixtures in `tests/conftest.py`.
- `config/`: Home Assistant runtime config mounted into the dev container (`/config`).
- `dev/`: dev-only assets (Frigate dev config, card harness).
- `PLANS.md`: architecture and v1 scope decisions.

## Build, Test, and Development Commands
- Tests: `~/.cache/frigate-feed-venv/bin/pytest -q` (create the venv with
  `python3 -m venv ~/.cache/frigate-feed-venv && ~/.cache/frigate-feed-venv/bin/pip install -r requirements_test.txt`).
- `docker compose -p frigate-feed-dev up -d`: start the dev stack (HA on http://localhost:8124).
- `docker compose -p frigate-feed-dev restart homeassistant`: reload after backend code changes.
- `./scripts/bump-card-resource-version.sh`: bump the Lovelace resource `?v=` after card changes.

## Safety
- This machine also runs production containers (`home-assistant` on :8123, `frigate`, `mosquitto` on :1883).
  Never modify, restart or reconfigure them; only read-only GET requests against production Frigate.
- Never bind host ports 8123, 1883, 5000 or 8971 from the dev stack.
- Never commit real Frigate payloads (camera names, zones, snapshots): fixtures are hand-made.

## Coding Style & Naming Conventions
- Language: Python (Home Assistant async style). Card: plain custom elements, no build step.
- Indentation: 4 spaces; keep code ASCII unless file already requires Unicode.
- Prefer explicit, small functions and typed signatures.
- Never import `custom_components.frigate`; read its runtime data via `frigate_source.py`.
- Naming: `snake_case.py`, `UPPER_SNAKE_CASE` constants, `snake_case` functions, `PascalCase` classes.
- Follow Home Assistant patterns (`async_*`, config entries, websocket schemas).

## Commit & Pull Request Guidelines
- Format: `type(scope): short summary` (for example, `feat(feed): map frigate events to items`).
- PRs should include what changed and why, how it was tested, config/runtime impact, and
  screenshots when UI behavior changes.

## Release Check
- User-facing changes need a semantic version bump in `custom_components/frigate_feed/manifest.json`,
  a matching `vX.Y.Z` tag, and a published GitHub release.
