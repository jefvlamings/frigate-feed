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
| Detection source | `binary_sensor.*_persoon` etc. state changes, burst merge (20s) | Frigate **review items** (`GET /api/review`) plus their events, one review item = one item (since v0.2.0; v0.1 used one event per item) |
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
  frigate_source.py  NEW: read-only adapter over the Frigate integration (client, config, entities)
  mqtt_listener.py   NEW: frigate/reviews -> debounced live-update signal
  feed.py            NEW (much smaller than reolink's 2100 lines): FrigateFeedManager
  media.py           NEW: authenticated HA proxy view for snapshot/clip + URL signing
  models.py          DetectionItem (copied, + optional `frigate` dict)
  storage.py         Store for hidden ids
  frontend/frigate-feed-card.js   ported card
  translations/en.json, nl.json, strings.json
  tests/
```

### Frigate connection (reuse the official Frigate integration)
The official **Frigate integration** (`frigate` domain, v5.15.6) is required. We do **not**
write our own Frigate HTTP client or auth: the integration stores per config entry
`hass.data["frigate"][entry_id] = {"client": FrigateApiClient, "config": <api/config>, ...}`.
`FrigateApiClient` already handles login/JWT, basic and bearer auth (any port, incl. 8971).
1. Manifest: `dependencies: ["http"]`, `after_dependencies: ["frigate", "lovelace", "mqtt"]`.
   "Required" is enforced at runtime: the config flow aborts with `no_frigate`, setup raises
   `ConfigEntryNotReady` until the Frigate entry's runtime data exists. (Keeps tests/hassfest
   independent of the external integration.)
2. Config flow picks a Frigate config entry (auto when only one); entry data `{frigate_entry_id}`.
3. `frigate_source.py` reads the client/config via string keys (never imports
   `custom_components.frigate`): `client.async_get_events(...)`, `client.async_get_event(id)`,
   `client.get_auth_headers()`, entry data `url` / `validate_ssl`, `config["mqtt"]["topic_prefix"]`,
   per-camera `record.alerts|detections.pre_capture`.
4. History/logbook links use entity registry unique ids `"{entry_id}:occupancy_sensor:{cam}_{label}"`,
   fallback `"{entry_id}:camera:{cam}"`.
Single instance (like reolink-feed).

### Ingestion: pull + MQTT push for live refresh
- **Pull**: `frigate_feed/list` calls `GET /api/events?after=<now - retention>&limit=<max>&labels=...`
  with a short in-memory cache (~5–10s) so multiple cards/browsers don't hammer Frigate.
  No local item store needed → nothing to migrate, rebuild is trivial.
- **Push (v1)**: subscribe to MQTT `<frigate topic_prefix>/events` (prefix from the Frigate
  entry/config, default `frigate`) via HA's `mqtt` component. On `new`/`end` for a mapped label,
  invalidate the cache and send dispatcher signal `frigate_feed_updated` (debounced ~2s).
  Non-admin users cannot `subscribe_events` to custom events, so the card uses the custom
  subscription `hass.connection.subscribeMessage(cb, {type: "frigate_feed/subscribe"})` and calls
  `_loadItems()` (keeping page/filter/dialog state). Unsubscribe on `disconnectedCallback`.

### Media serving (decided: proxy, no copying)
Register one authenticated `HomeAssistantView` at `/api/frigate_feed/media/{event_id}/{kind}`
(`kind` = `snapshot.jpg` | `clip.mp4`), streaming `{url}/api/events/{id}/{kind}` with the Frigate
client's auth headers. Range headers are forwarded, but note Frigate's `clip.mp4` is a fragmented
MP4 stream without Range support, so seeking is limited to buffered data. We don't use the Frigate
integration's own notification proxy because it depends on an option that is off by default.

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
- This machine also runs **production** HA (:8123, host network), Frigate 0.18.2 and mosquitto
  (:1883). The dev stack must never touch them or bind those ports.
- `docker-compose.yml` (project `frigate-feed-dev`, bridge network): HA 2026.10.4 published on
  **8124**, **Frigate 0.18.2** and an unpublished mosquitto:
  - Frigate without auth on internal :5000 (auth is handled by the Frigate integration's client,
    so frigate_feed doesn't need to exercise it), one or two fake cameras fed by go2rtc/ffmpeg
    looping sample videos (people, a dog, a car) so all three label buckets produce events;
    detector `cpu`; recording + snapshots enabled with short retention.
  - mosquitto shared by Frigate and HA's MQTT integration.
  - Official Frigate integration v5.15.6 cloned into `config/custom_components/frigate`,
    pointing at `http://frigate-dev:5000`.
- Fake camera media lives in `dev/media/` (gitignored, fetched by a script).
- `scripts/bump-card-resource-version.sh`: ported with renamed paths.
- `AGENTS.md`, `README.md`, `hacs.json`, `.github/workflows/hassfest.yml`: ported.

---

## Phases
1. **Scaffold** — repo skeleton for `github.com/jefvlamings/frigate-feed` (manifest
   `frigate_feed` v0.1.0, hacs.json, AGENTS.md) with frozen module contracts so the units below
   can be built in parallel.
2. **Frigate source adapter** — `frigate_source.py`: read-only access to the official Frigate
   integration's runtime data (`hass.data["frigate"][entry_id]`: client, cached config, entry
   `url`), event fetch via the integration's client (its auth, any port), MQTT topic prefix,
   per-camera `pre_capture`, occupancy/camera entity lookup via the entity registry. No own
   HTTP client or JWT handling; never imports `custom_components.frigate`.
3. **Feed manager + WS API** — `feed.py`: event → `DetectionItem` mapping, label bucketing,
   `recording` status (`pending` while `end_time` is null, `linked` once `has_clip`), short list
   cache, hidden-id store (`storage.py`); `frigate_feed/list`, `resolve_recording`,
   `delete_item` (hide only).
4. **Media proxy** — `media.py`: authenticated `/api/frigate_feed/media/{event_id}/{kind}` view
   streaming snapshot/clip from Frigate with the integration's auth headers; signed URLs.
5. **MQTT listener** — `mqtt_listener.py`: `<topic_prefix>/events` subscription → cache
   invalidation + debounced `frigate_feed_updated`, exposed to the card via
   `frigate_feed/subscribe`.
6. **Config + options flow** — pick the Frigate config entry (abort `no_frigate`), options for
   labels / retention / max detections; en + nl translations.
7. **Card port** — copy + renames + changes above; verify side-by-side against reolink-feed in
   the dev HA (desktop + mobile width, dialog, timeline clusters, share/download, live refresh).
8. **Dev environment + CI** — docker-compose (`frigate-feed-dev`: HA :8124, Frigate 0.18.2 with
   two go2rtc fake cameras looping sample clips, mosquitto, all on a bridge network), media
   fetch + Frigate integration install scripts; hassfest, HACS and pytest workflows; README.
9. **Tests** — pytest: event mapping, label bucketing, status transitions, list filtering
   (retention/labels/hidden), media proxy, MQTT handler debounce, config flow; Frigate
   integration runtime data faked in `tests/conftest.py`.
10. **Release** — end-to-end check in the dev stack, HACS validation, hassfest, tag `v0.1.0`,
    GitHub release.

---

## Decisions (agreed 2026-10-26)
| Topic | Decision |
|---|---|
| Media | Proxy from Frigate via signed HA URLs; no local copies; Frigate retention applies |
| Item granularity | One Frigate **event** = one item |
| Burst merge | Frigate's own grouping: objects detected together on a camera share one review item (v0.2.0) |
| Delete | Hide only; never modifies Frigate |
| Labels | Bucket into person/pet/vehicle; drop motion/visitor; raw label kept in `item.frigate` |
| Connection | Required official Frigate integration; reuse its client/auth and cached config |
| Live refresh | MQTT `frigate/reviews` → dispatcher → `frigate_feed/subscribe` WS subscription |
| Dev env | Local Frigate + fake cameras + mosquitto in docker-compose |
| Packaging | Separate HACS repo `jefvlamings/frigate-feed`, starting v0.1.0 |
| Card | One-time fork of reolink-feed card; free to diverge afterwards |
| v1 extras | MQTT auto-refresh only (no camera option, min score, or "Open in Frigate" link) |
