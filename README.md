# Frigate Feed

Home Assistant custom integration + Lovelace card with a chronological detection timeline
(person / pet / vehicle) sourced from [Frigate](https://frigate.video) events. The UI replicates
[reolink-feed](https://github.com/jefvlamings/reolink-feed).

Status: in development (v0.1.0). Architecture and decisions: see `PLANS.md`.

## Requirements
- Home Assistant 2026.10+
- The official [Frigate integration](https://github.com/blakeblackshear/frigate-hass-integration) set up
- MQTT integration connected to the same broker as Frigate (for live refresh)

## Card

```yaml
type: custom:frigate-feed-card
```

## Development

```bash
python3 -m venv ~/.cache/frigate-feed-venv
~/.cache/frigate-feed-venv/bin/pip install -r requirements_test.txt
~/.cache/frigate-feed-venv/bin/pytest -q
```
