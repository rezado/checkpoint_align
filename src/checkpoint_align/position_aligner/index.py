"""Internal sparse dynamic-event index."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .protocol import BuildRun, ProgressEvent, validate_events, validate_run


class EventIndex:
    def __init__(self, run: BuildRun, events: Sequence[ProgressEvent | Mapping[str, Any]]):
        self.run = validate_run(run)
        self.events = validate_events(events, self.run)
        self.by_key: dict[str, tuple[ProgressEvent, ...]] = {}
        grouped: dict[str, list[ProgressEvent]] = defaultdict(list)
        for event in self.events:
            grouped[event.semantic_key].append(event)
        self.by_key = {key: tuple(value) for key, value in grouped.items()}

    def find(self, anchor_id: str, occurrence: int) -> ProgressEvent | None:
        return next((event for event in self.events if event.anchor_id == anchor_id and event.occurrence == occurrence), None)

    def to_dict(self) -> dict:
        return {"build_id": self.run.build_id, "run_id": self.run.run_id, "event_count": len(self.events), "keys": {key: len(value) for key, value in self.by_key.items()}}
