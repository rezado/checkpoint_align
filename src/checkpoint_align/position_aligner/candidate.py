"""Semantic candidate generation; coarse BBV features are intentionally absent."""

from __future__ import annotations

from typing import Iterable

from .protocol import ProgressEvent


def semantic_candidates(source: ProgressEvent, target_events: Iterable[ProgressEvent], correspondence=None) -> list[tuple[ProgressEvent, float, str]]:
    allowed = set((correspondence or {}).get(source.anchor_id, ()))
    result = []
    for target in target_events:
        if target.anchor_id == source.anchor_id:
            result.append((target, 3.0, "anchor_id"))
        elif target.anchor_id in allowed or target.semantic_key == source.semantic_key:
            result.append((target, 2.0, "semantic_key"))
    return result
