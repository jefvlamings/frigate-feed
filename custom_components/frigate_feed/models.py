"""Data models for the Frigate feed integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class DetectionItem:
    """Normalized timeline item (one Frigate review item).

    The shape matches reolink-feed so the card can stay close to it; `label` is the
    main label, `labels` every label in the review and `detections` its Frigate events. `snapshot_url` and
    `recording["local_url"]` hold unsigned media proxy paths; they are signed per
    websocket response by `media.sign_item_urls`.
    """

    id: str
    start_ts: str
    end_ts: str | None
    duration_s: int | None
    label: str
    source_entity_id: str
    camera_name: str
    snapshot_url: str | None
    recording: dict[str, Any]
    frigate: dict[str, Any] | None = None
    thumbnail_url: str | None = None
    labels: list[str] = field(default_factory=list)
    detections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start_ts)

    @property
    def end_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.end_ts) if self.end_ts else None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DetectionItem":
        return cls(
            id=data["id"],
            start_ts=data["start_ts"],
            end_ts=data.get("end_ts"),
            duration_s=data.get("duration_s"),
            label=data["label"],
            source_entity_id=data.get("source_entity_id", ""),
            camera_name=data["camera_name"],
            snapshot_url=data.get("snapshot_url"),
            recording=data.get("recording", {"status": "pending"}),
            frigate=data.get("frigate"),
            thumbnail_url=data.get("thumbnail_url"),
            labels=list(data["labels"]) if "labels" in data else [data["label"]],
            detections=list(data.get("detections") or []),
        )
