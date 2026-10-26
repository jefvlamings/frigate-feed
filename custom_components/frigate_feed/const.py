"""Constants for the Frigate feed integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "frigate_feed"
FRIGATE_DOMAIN = "frigate"
STORAGE_VERSION = 1
STORAGE_KEY_HIDDEN = f"{DOMAIN}.hidden"
CARD_FILENAME = "frigate-feed-card.js"
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"

CONF_FRIGATE_ENTRY_ID = "frigate_entry_id"
CONF_ENABLED_LABELS = "enabled_labels"
CONF_RETENTION_HOURS = "retention_hours"
CONF_MAX_DETECTIONS = "max_detections"

DEFAULT_RETENTION_HOURS = 24
MIN_RETENTION_HOURS = 1
MAX_RETENTION_HOURS = 168
DEFAULT_MAX_DETECTIONS = 100
MIN_MAX_DETECTIONS = 10
MAX_MAX_DETECTIONS = 2000
LIST_ITEMS_LIMIT = 200
# Upper bound on the events fetched to describe the detections of listed review items.
EVENTS_QUERY_LIMIT = 1000

CLEANUP_INTERVAL_SECONDS = 3600
LIST_CACHE_SECONDS = 10
MQTT_DEBOUNCE_SECONDS = 2.0
DEFAULT_PRE_CAPTURE_SECONDS = 5
# Frigate writes recording segments in ~10s chunks; an event clip returns HTTP 400 until
# the segments covering end_time + post_capture exist.
CLIP_READY_GRACE_SECONDS = 15
SIGNED_URL_EXPIRY = timedelta(hours=24)

SIGNAL_FEED_UPDATED = "frigate_feed_updated"

# Media proxy served by this integration; kinds are MEDIA_KINDS.
MEDIA_URL = "/api/frigate_feed/media/{event_id}/{kind}"
MEDIA_KIND_SNAPSHOT = "snapshot.jpg"
MEDIA_KIND_CLIP = "clip.mp4"
MEDIA_KINDS: tuple[str, ...] = (MEDIA_KIND_SNAPSHOT, MEDIA_KIND_CLIP)
# Review item media: the recording clip spanning the review, and Frigate's review thumbnail.
REVIEW_MEDIA_URL = "/api/frigate_feed/media/review/{review_id}/{kind}"
REVIEW_MEDIA_KIND_THUMB = "thumb.webp"
REVIEW_MEDIA_KINDS: tuple[str, ...] = (MEDIA_KIND_CLIP, REVIEW_MEDIA_KIND_THUMB)

SUPPORTED_DETECTION_LABELS: tuple[str, ...] = ("person", "pet", "vehicle")
DEFAULT_ENABLED_DETECTION_LABELS: tuple[str, ...] = SUPPORTED_DETECTION_LABELS
# A review item with several labels is shown under the first of these.
LABEL_PRIORITY: tuple[str, ...] = ("person", "vehicle", "pet")

# Frigate object label -> feed label bucket. Unlisted labels are dropped.
FRIGATE_LABEL_BUCKETS: dict[str, str] = {
    "person": "person",
    "dog": "pet",
    "cat": "pet",
    "bird": "pet",
    "horse": "pet",
    "sheep": "pet",
    "cow": "pet",
    "bear": "pet",
    "deer": "pet",
    "elephant": "pet",
    "zebra": "pet",
    "giraffe": "pet",
    "goat": "pet",
    "rabbit": "pet",
    "raccoon": "pet",
    "fox": "pet",
    "squirrel": "pet",
    "skunk": "pet",
    "car": "vehicle",
    "truck": "vehicle",
    "motorcycle": "vehicle",
    "bicycle": "vehicle",
    "bus": "vehicle",
    "boat": "vehicle",
    "train": "vehicle",
    "airplane": "vehicle",
    "license_plate": "vehicle",
}


def bucket_frigate_label(label: str | None) -> str | None:
    """Map a raw Frigate label onto a feed label bucket, or None to drop it."""
    lowered = str(label or "").strip().lower()
    # Review items list objects confirmed by a sub label as e.g. "person-verified".
    lowered = lowered.removesuffix("-verified")
    return FRIGATE_LABEL_BUCKETS.get(lowered)


def frigate_labels_for_buckets(buckets: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    """Return the raw Frigate labels that map onto the given buckets (sorted)."""
    wanted = set(buckets)
    return sorted(label for label, bucket in FRIGATE_LABEL_BUCKETS.items() if bucket in wanted)
