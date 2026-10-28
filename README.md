# Frigate Feed

Home Assistant custom integration + bundled Lovelace card with a chronological detection
timeline sourced from [Frigate](https://frigate.video) review items (alerts and detections).

## Project Goal

Ship the same card experience as [reolink-feed](https://github.com/jefvlamings/reolink-feed)
(card layout, timeline clustering, info dialog, prev/next navigation, download/share,
history/logbook links, i18n, editor), but powered by Frigate:

- one Frigate **review item** = one feed item: every object detected together on a camera
  (e.g. a person walking a dog past a car) shares one item and one recording, exactly like
  Frigate's own Review page; the dialog has a chip per detection that jumps to it in the clip
- Frigate labels bucketed into `person`, `pet` (dog, cat, bird, ...) and `vehicle` (car, truck, bicycle, ...)
- snapshot and clip for every item, proxied from Frigate through Home Assistant (no local copies)
- media URLs stay stable and finished items' snapshots/thumbnails are browser-cacheable;
  the feed updates when the page is reloaded (no live refresh)
- "delete" only hides an item in the feed; Frigate is never modified

Architecture and decisions are documented in [`PLANS.md`](PLANS.md).

## Requirements

- Home Assistant 2026.10 or newer
- The official [Frigate integration](https://github.com/blakeblackshear/frigate-hass-integration)
  (`frigate` domain, tested with v5.15.6) set up and connected to your Frigate server.
  Frigate Feed reuses its client and authentication; it has no Frigate URL/credentials of its own.
- Frigate with snapshots and recordings enabled (clips come from Frigate's recordings, so
  Frigate's own retention decides how long items stay available).

## Installation (HACS)

1. In HACS, add `https://github.com/jefvlamings/frigate-feed` as a custom repository of type `Integration`.
2. Install **Frigate Feed** and restart Home Assistant.
3. `Settings -> Devices & Services -> Add Integration -> Frigate Feed`.
   If you have more than one Frigate server configured, pick the one to use.
4. Refresh the browser. In storage-mode dashboards the card resource
   `/frigate_feed/frigate-feed-card.js` is added automatically. For YAML-mode dashboards, add it
   as a Lovelace resource of type `module`.

Options (`Configure` on the integration): enabled labels (person/pet/vehicle), retention hours
and maximum number of detections.

## Lovelace Card

```yaml
type: custom:frigate-feed-card
labels:
  - person
  - pet
  - vehicle
cameras:
  - front_door
  - driveway
page_size: 20
show_timeline: true
thumbnail: object
```

| Option | Default | Description |
|---|---|---|
| `labels` | all enabled labels | Label buckets to show (`person`, `pet`, `vehicle`). |
| `cameras` | all cameras | Only show detections from these cameras. |
| `page_size` | `20` | Items per page. |
| `show_timeline` | `true` | Show the timeline header with clustered detections. |
| `thumbnail` | `object` | Grid thumbnails: `object` crops the snapshot to the detected object (like Frigate's Explore page), `frame` shows the whole frame centred on the object. The dialog always shows the full snapshot. |

The card is also available in the card picker and has a visual editor.

## WebSocket API

| Command | Request | Response |
|---|---|---|
| `frigate_feed/list` | `labels` (optional list of buckets) | `{ items, enabled_labels, retention_hours }`, newest first |
| `frigate_feed/resolve_recording` | `item_id`, `final_attempt` (optional bool) | the item's `recording` (`status`: `linked`, `pending`, `not_found`) |
| `frigate_feed/delete_item` | `item_id` | hides the item (Frigate is not modified) |

Items keep the reolink-feed shape (`id`, `start_ts`, `end_ts`, `duration_s`, `label`,
`source_entity_id`, `camera_name`, `snapshot_url`, `recording`) plus:

- `id`: the Frigate review item id; `label`: the main label (person, then vehicle, then pet)
- `labels`: every enabled label in the review item
- `detections`: its Frigate events (`event_id`, `label`, `frigate_label`, `sub_label`, `score`,
  `start_offset_s` into the clip, `duration_s`, `has_snapshot`), oldest first
- `frigate`: review id, best event id, severity, camera, objects, sub labels, zones and `box`
  (the object box on the snapshot, `[x, y, w, h]` fractions; used to centre `frame` thumbnails)
- `thumbnail_url`: the best detection's snapshot cropped to the object (`snapshot.jpg?crop=1&bbox=0&height=400&quality=80`), or `null`

`snapshot_url` and `recording.local_url` are signed Home Assistant paths to the media proxy:
`/api/frigate_feed/media/{event_id}/snapshot.jpg` (best detection) or
`/api/frigate_feed/media/review/{review_id}/thumb.webp` (fallback), and
`/api/frigate_feed/media/review/{review_id}/clip.mp4`: the camera recording over the review
window plus the camera's pre/post capture.

Frigate serves clips as fragmented MP4 without byte-range support, which browsers need to
know the clip length and to seek. When a browser asks for a range, the proxy downloads the clip
once into a temporary file in the Home Assistant container (`/tmp/frigate_feed_clips`, up to
512 MB per clip and 1 GB in total, removed 5 minutes after the last request and on restart) and
serves ranges from there. Long clips therefore take a few seconds before they start playing.

## Development

### Dev stack

`docker-compose.yml` runs an isolated stack (project `frigate-feed-dev`, own bridge network):

| Service | Container | Access |
|---|---|---|
| `homeassistant` (2026.10.4) | `frigate-feed-dev-ha` | http://localhost:8124 |
| `frigate-dev` (Frigate 0.18.2, CPU detector, no auth) | `frigate-feed-dev-frigate` | `http://frigate-dev:5000` inside the network only |
| `mosquitto-dev` (anonymous) | `frigate-feed-dev-mosquitto` | `mosquitto-dev:1883` inside the network only |

Frigate's two fake cameras (`front_door`, `driveway`) loop local sample clips via go2rtc
(config: `dev/frigate/config.yml`). Frigate data is stored in `dev/frigate/storage/` (gitignored).

1. Fetch the sample clips into `dev/media/` (gitignored; Intel IoT DevKit CC BY 4.0 clips and a
   CC BY-SA 3.0 Wikimedia Commons dog clip, see the script header for attribution). Transcoding
   runs in a throwaway container from the Frigate image:

   ```bash
   ./scripts/fetch-dev-media.sh
   ```

2. Install the official Frigate integration (v5.15.6) into `config/custom_components/frigate` (gitignored):

   ```bash
   ./scripts/install-frigate-integration.sh
   ```

3. Start the stack (always pass the project name):

   ```bash
   docker compose -p frigate-feed-dev up -d
   docker compose -p frigate-feed-dev logs -f homeassistant
   ```

4. Open http://localhost:8124, complete onboarding, then in `Settings -> Devices & Services`:
   1. Add **MQTT**: broker `mosquitto-dev`, port `1883`, no username/password.
   2. Add **Frigate**: URL `http://frigate-dev:5000` (no credentials).
   3. Add **Frigate Feed**.
5. Add the card to a dashboard. Within a few minutes Frigate produces person, car and dog events.

Useful commands:

```bash
# Reload after backend changes
docker compose -p frigate-feed-dev restart homeassistant
# Bust the browser cache after card changes (bumps the Lovelace resource ?v=, no restart)
HA_TOKEN=<long-lived token> ./scripts/bump-card-resource-version.sh
# Inspect Frigate
docker compose -p frigate-feed-dev exec frigate-dev curl -s localhost:5000/api/version
docker compose -p frigate-feed-dev exec frigate-dev curl -s 'localhost:5000/api/review?limit=5'
# Tear down and remove Frigate dev data (files are root-owned, so delete from a container)
docker compose -p frigate-feed-dev down -v
docker run --rm -v "$PWD/dev/frigate:/d" --entrypoint /bin/rm eclipse-mosquitto:2 -rf /d/storage
```

To look at the Frigate UI, uncomment the loopback-only port mapping (`127.0.0.1:5001:5000`)
in `docker-compose.yml`.

### Tests

```bash
python3 -m venv ~/.cache/frigate-feed-venv
~/.cache/frigate-feed-venv/bin/pip install -r requirements_test.txt
~/.cache/frigate-feed-venv/bin/pytest -q
```

CI runs hassfest, HACS validation and pytest (Python 3.14) on every pull request.

### Safety

The development machine may also run **production** Home Assistant, Frigate and mosquitto
containers. The dev stack therefore:

- always runs under the compose project `frigate-feed-dev` with `frigate-feed-dev-*` container names;
- uses its own bridge network (no host networking);
- never binds host ports 8123, 1883, 5000 or 8971 (only HA on 8124).

Never restart, modify or reconfigure containers outside this project, and only make read-only
GET requests against a production Frigate. Don't commit Frigate payloads, snapshots or the
downloaded dev media.

## Release

User-facing changes bump the version in `custom_components/frigate_feed/manifest.json`, get a
matching `vX.Y.Z` tag and a published GitHub release (HACS installs from releases).
