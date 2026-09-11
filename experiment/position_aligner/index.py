"""Internal sparse dynamic-event index."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .protocol import BuildRun, ProgressEvent, validate_events, validate_run


class EventIndex:
    def __init__(self, run: BuildRun, events: Sequence[ProgressEvent | Mapping[str, Any]]):
        self.run = validate_run(run)
        self.events = validate_events(events, self.run)
        self.by_key: dict[tuple[str, str], tuple[ProgressEvent, ...]] = {}
        grouped: dict[tuple[str, str], list[ProgressEvent]] = defaultdict(list)
        for event in self.events:
            grouped[(event.semantic_key, event.event_phase)].append(event)
        self.by_key = {key: tuple(value) for key, value in grouped.items()}

    def find(self, anchor_id: str, occurrence: int, phase: str) -> ProgressEvent | None:
        return next((event for event in self.events if event.anchor_id == anchor_id and event.occurrence == occurrence and event.event_phase == phase), None)

    def to_dict(self) -> dict:
        return {"build_id": self.run.build_id, "run_id": self.run.run_id, "event_count": len(self.events), "keys": {f"{key[0]}|{key[1]}": len(value) for key, value in self.by_key.items()}}
