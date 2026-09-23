"""Semantic candidate generation; coarse BBV features are intentionally absent."""

from __future__ import annotations

from typing import Iterable, Mapping

from .protocol import ProgressEvent


def semantic_candidates(source: ProgressEvent, target_events: Iterable[ProgressEvent], correspondence=None) -> list[tuple[ProgressEvent, float, str]]:
    allowed = set((correspondence or {}).get(source.anchor_id, ()))
    result = []
    for target in target_events:
        if not _contexts_compatible(source.context, target.context):
            continue
        if target.anchor_id == source.anchor_id:
            result.append((target, 3.0 + _context_score(source.context, target.context), "anchor_id"))
        elif target.anchor_id in allowed or target.semantic_key == source.semantic_key:
            result.append((target, 2.0 + _context_score(source.context, target.context), "semantic_key"))
    return result


def _contexts_compatible(source: object, target: object) -> bool:
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return True
    return all(key not in target or target[key] == value for key, value in source.items())


def _context_score(source: object, target: object) -> float:
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return 0.0
    return 0.5 if source and set(source) & set(target) else 0.0
