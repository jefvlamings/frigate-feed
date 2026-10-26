# Frigate Feed (Home Assistant) — Implementation Plan

## Goal
Ship a Home Assistant custom integration + bundled Lovelace card that looks and behaves
**exactly like `reolink-feed`** (same card layout, timeline, clustering, info dialog,
prev/next navigation, downloads/share, history/logbook links, i18n, editor), but sources
detections, snapshots and recordings from **Frigate** instead of Reolink binary sensors and
the Reolink Media Browser tree.

Reference implementation: `../reolink-feed` (v0.2.8).

---

## Core design principle: keep the card contract identical
The card only talks to the backend through three WebSocket commands and one item shape.
If the Frigate backend returns the **same payload**, the card can be copied almost verbatim.

Card → backend calls (from `reolink-feed-card.js`):
| Reolink command                 | Frigate Feed command            | Notes |
|---------------------------------|---------------------------------|-------|
| `reolink_feed/list`             | `frigate_feed/list`             | returns `items`, `enabled_labels`, `retention_hours` |
| `reolink_feed/resolve_recording`| `frigate_feed/resolve_recording`| re-check one item's clip; `final_attempt` accepted |
| `reolink_feed/delete_item`      | `frigate_feed/delete_item`      | see "Delete semantics" |
| `reolink_feed/rebuild_from_history` | `frigate_feed/rebuild` (optional) | re-sync from Frigate API |

Item shape the card depends on (must be preserved):
```json
{
  "id": "1730000000.123456-abc123",
  "start_ts": "2026-10-26T17:36:30+01:00",
  "end_ts": "2026-10-26T17:37:06+01:00",
  "duration_s": 36,
  "label": "person",
  "source_entity_id": "binary_sensor.voordeur_person_occupancy",
  "camera_name": "Voordeur",
  "snapshot_url": "<url usable in <img>>",
  "recording": {
    "status": "linked|pending|not_found|download_failed",
    "local_url": "<url usable in <video> and fetch()>",
    "clip_start_ts": "...",
    "clip_end_ts": "...",
    "start_offset_s": 5.0,
    "resolved_at": "..."
  },
  "frigate": { "event_id": "...", "label": "dog", "sub_label": null, "score": 0.87, "zones": [] }
}
```
`frigate` is an additive field (ignored by the ported card unless we use it).

---

## Data source mapping (Reolink → Frigate)

| Concern | Reolink Feed | Frigate Feed |
|---|---|---|
| Detection source | `binary_sensor.*_persoon` etc. state changes, burst merge (20s) | Frigate **events** (`GET /api/events`), one tracked object = one item |
| Item id | uuid | Frigate event id (stable, dedupes for free) |
| Start/end | event bus timestamps | `start_time` / `end_time` (epoch → local ISO) |
| In-progress | sensor `on` | `end_time == null` → `recording.status = pending` |
| Snapshot | capture camera entity image after 1s delay | `GET /api/events/{id}/snapshot.jpg` (Frigate already has best frame) |
| Recording | scrape Media Browser `Low resolution/Y/M/D/Person`, match by time, download & trim | `GET /api/events/{id}/clip.mp4` once `has_clip` is true |
| Seek offset | event start − clip start | ≈ Frigate `record.events.pre_capture` (default 5s), read from `/api/config` per camera |
| Retention | own pruning + storage cap | Frigate owns retention; we only filter by `retention_hours` |
| Rebuild | recorder history replay | just re-query the Frigate API |
| History/Logbook links | Reolink binary sensor | Frigate integration's `binary_sensor.<camera>_<label>_occupancy` (resolved via entity registry), fallback `camera.<camera>` |

### Label mapping
The card has a fixed label set (`person, pet, vehicle, motion, visitor`) with icons + NL/EN
strings. Map Frigate object labels onto it so the UI stays identical:
- `person` → `person`
- `dog, cat, bird, horse, sheep, cow, bear, deer, ...` → `pet`
- `car, truck, motorcycle, bicycle, bus, boat, license_plate` → `vehicle`
- everything else (`package`, ...) → configurable: drop by default
- `motion` / `visitor`: not produced by Frigate events → hidden from options (or `visitor`
  later from Frigate face/sub_label or a doorbell entity — out of scope v1)

Keep the raw Frigate label in `item.frigate.label` (could later be shown as the overlay text).

---

## Architecture

```
custom_components/frigate_feed/
  __init__.py        setup/unload, WS commands, card static path + Lovelace resource auto-add (ported)
  config_flow.py     pick Frigate source + options (labels, cameras, retention, max detections)
  const.py           domain, defaults, label mapping tables
  frigate_api.py     NEW: thin async client (aiohttp via HA session): version, config, events, event, media
  feed.py            NEW (much smaller than reolink's 2100 lines): FrigateFeedManager
  media.py           NEW: authenticated HA proxy views for snapshot/clip
  models.py          DetectionItem (copied, + optional `frigate` dict)
  storage.py         Store for hidden/deleted ids (+ optional item cache)
  frontend/frigate-feed-card.js   ported card
  translations/en.json, nl.json, strings.json
  tests/
```

### Frigate connection (config flow)
Target: Frigate **0.14+ on the authenticated port 8971**, connected to HA via the official
**Frigate integration** (`frigate` domain).
1. Manifest: `"dependencies": ["frigate", "mqtt"]`.
2. Config flow lists the Frigate integration's config entries (auto-select if only one) and
   reuses its `url` + `username`/`password`. Abort with a clear message if none exist.
3. Auth: `POST /api/login` → JWT (`frigate_token` cookie / bearer); refresh on 401.
   Self-signed TLS on 8971 is common → honour the Frigate entry's SSL-verify setting.
4. Validate with `GET /api/version`, read `GET /api/config` for camera list + `pre_capture`.
Single instance (like reolink-feed).

### Ingestion: pull + MQTT push for live refresh
- **Pull**: `frigate_feed/list` calls `GET /api/events?after=<now - retention>&limit=<max>&labels=...`
  with a short in-memory cache (~5–10s) so multiple cards/browsers don't hammer Frigate.
  No local item store needed → nothing to migrate, rebuild is trivial.
- **Push (v1)**: subscribe to MQTT `<frigate topic_prefix>/events` (prefix from the Frigate
  entry/config, default `frigate`) via HA's `mqtt` component. On `new`/`end` for a mapped label,
  invalidate the cache and fire HA event `frigate_feed_updated` (debounced ~2s).
  Card subscribes with `hass.connection.subscribeEvents(..., "frigate_feed_updated")` and calls
  `_loadItems()` (keeping page/filter/dialog state). Unsubscribe on `disconnectedCallback`.

### Media serving (decided: proxy, no copying)
Register two authenticated `HomeAssistantView`s:
- `/api/frigate_feed/{event_id}/snapshot.jpg`
- `/api/frigate_feed/{event_id}/clip.mp4` (stream through, forward `Range` headers so `<video>` seeking works)

The backend returns them as **signed paths** (`async_sign_path`, e.g. 24h expiry) in
`snapshot_url` / `recording.local_url`, so `<img>`, `<video>` and the card's `fetch()`-based
share/download work unchanged. Frigate's address/credentials never reach the browser.

Items disappear from the feed once Frigate's own retention removes them; that is accepted.
The proxy authenticates to Frigate with the JWT from the connection layer.

### Recording status logic
- `end_time is null` → `pending`
- ended + `has_clip` → `linked`, `local_url` = signed clip URL, `start_offset_s` = pre_capture
- ended + no clip (recording disabled / retained-only-on-alerts) → `not_found`
- `resolve_recording` = re-fetch `GET /api/events/{id}`; `final_attempt` kept for API compat.
- clip proxy error → `download_failed` (keeps the card's existing toast/labels meaningful)

### Delete semantics
Decided: **hide only**. Frigate data is never modified.
- `delete_item` stores the event id in `Store` (`frigate_feed.hidden`), filtered out on list.
- Hidden ids older than retention are pruned on startup/hourly so the store stays small.

---

## Card port (one-time fork, then evolves independently)
1. Copy `reolink-feed-card.js` (v0.2.8) → `frontend/frigate-feed-card.js`; this starts as an exact visual copy.
2. Renames: `reolink-feed-card` → `frigate-feed-card`, `ReolinkFeedCard*` → `FrigateFeedCard*`,
   `reolink_feed/*` → `frigate_feed/*`, card picker name/description.
3. Functional changes:
   - Label set `person, pet, vehicle` (remove `motion`, `visitor` from `SUPPORTED_CARD_LABELS`, i18n kept harmless).
   - `_mediaFolderDisplayPath` / `_mediaFileDisplayPath`: show `Frigate/<camera>/<event_id>` instead of the Reolink tree.
   - Download filenames: `<camera>_<label>_<HHMMSS>.jpg/.mp4`.
   - Live refresh: subscribe to `frigate_feed_updated` (see Ingestion).
   - Signed URLs expire: on `<img>`/`<video>` error, reload items once to get fresh signatures.
4. No sync script; after the fork the two cards may diverge.

---

## Options flow (ported, trimmed)
Keep: enabled labels (person/pet/vehicle), retention hours, max detections.
Drop: `max_storage_gb` (no local media) and "rebuild now" (feed is always live from Frigate).
Camera filtering stays as the card's existing `cameras:` YAML option (no backend option in v1).

---

## Dev environment
- `docker-compose.yml`: HA (same as reolink-feed) **plus Frigate 0.14+ and mosquitto**:
  - Frigate with auth enabled on 8971, one or two fake cameras fed by go2rtc/ffmpeg looping
    sample videos (people, a dog, a car) so all three label buckets produce events; detector `cpu`;
    recording + snapshots enabled with short retention.
  - mosquitto shared by Frigate and HA's MQTT integration.
  - HA config pre-wired: MQTT broker + official Frigate integration (installed via HACS or
    copied into `config/custom_components/frigate`), pointing at `https://frigate:8971`.
- Fake camera media lives in `dev/media/` (gitignored, fetched by a script).
- `scripts/bump-card-resource-version.sh`: ported with renamed paths.
- `AGENTS.md`, `README.md`, `hacs.json`, `.github/workflows/hassfest.yml`: ported.

---

## Phases
1. **Scaffold** — `git init`, repo skeleton for `github.com/jefvlamings/frigate-feed`
   (manifest `frigate_feed` v0.1.0, hacs.json, scripts, AGENTS.md, hassfest + HACS CI).
2. **Dev environment** — docker-compose with HA + Frigate (auth, fake cameras) + mosquitto;
   confirm Frigate produces person/pet/vehicle events and the Frigate integration is loaded.
3. **Frigate client + config flow** — `frigate_api.py` (JWT login on 8971, refresh on 401, TLS
   setting), pick Frigate integration entry, connection validation.
4. **Feed manager + WS API** — event → `DetectionItem` mapping, label bucketing, entity lookup for
   `source_entity_id`, `list` / `resolve_recording` / `delete_item` (hide), cache, hidden-id store.
5. **Media proxy** — snapshot + clip views with Range support, signed URLs.
6. **MQTT live refresh** — `frigate/events` subscription → debounced `frigate_feed_updated`.
7. **Card port** — copy + renames + changes above; verify side-by-side against reolink-feed in
   the dev HA (desktop + mobile width, dialog, timeline clusters, share/download, live refresh).
8. **Options flow + translations** — en/nl.
9. **Tests** — pytest: event mapping, label bucketing, status transitions, list filtering
   (retention/labels/hidden), MQTT handler debounce, config-flow helpers; fake Frigate API via
   aiohttp test server.
10. **Release** — README, HACS validation, hassfest, tag `v0.1.0`, GitHub release.

---

## Decisions (agreed 2026-10-26)
| Topic | Decision |
|---|---|
| Media | Proxy from Frigate via signed HA URLs; no local copies; Frigate retention applies |
| Item granularity | One Frigate **event** = one item |
| Burst merge | None; two simultaneous people = two items |
| Delete | Hide only; never modifies Frigate |
| Labels | Bucket into person/pet/vehicle; drop motion/visitor; raw label kept in `item.frigate` |
| Connection | Via the official Frigate integration's config entry; Frigate 0.14+ on 8971 (JWT auth) |
| Dev env | Local Frigate + fake cameras + mosquitto in docker-compose |
| Packaging | Separate HACS repo `jefvlamings/frigate-feed`, starting v0.1.0 |
| Card | One-time fork of reolink-feed card; free to diverge afterwards |
| v1 extras | MQTT auto-refresh only (no camera option, min score, or "Open in Frigate" link) |
