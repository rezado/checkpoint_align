"""Dynamic-event alignment implementation behind the small public interface."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from .candidate import semantic_candidates
from .index import EventIndex
from .protocol import (
    AlignmentResult,
    BuildRun,
    Correspondence,
    Position,
    ProgressEvent,
    validate_events,
    validate_position,
    validate_run,
)


@dataclass(frozen=True)
class _Path:
    score: float
    ops: tuple[tuple[int | None, int | None], ...]


class _SearchTruncated(Exception):
    pass


class PositionAligner:
    """Align one source position using complete sparse dynamic event traces."""

    def align(self, source_trace, target_trace, correspondence, source_position, policy=None) -> AlignmentResult:
        policy = dict(policy or {})
        if any("weight" in str(key).lower() for key in policy):
            raise ValueError("PositionAligner must not receive SimPoint weights")
        source_run, source_events, source_evidence = _trace(source_trace)
        target_run, target_events, target_evidence = _trace(target_trace)
        source_run = validate_run(source_run)
        target_run = validate_run(target_run)
        source_indexed = EventIndex(source_run, source_events)
        target_indexed = EventIndex(target_run, target_events)
        source_events = source_indexed.events
        target_events = target_indexed.events
        source = validate_position(source_position, source_run)
        if not _dynamic_evidence(source_evidence) or not _dynamic_evidence(target_evidence) or not source_events or not target_events:
            return _rejected(source, "EVIDENCE_COLLECTION_FAILED", source_run, target_run)
        if not _compatible(source_run, target_run) or source.build_id != source_run.build_id:
            return _rejected(source, "INCOMPATIBLE_RUN", source_run, target_run)
        source_index = next((index for index, event in enumerate(source_events) if _same_position(event, source)), None)
        if source_index is None:
            return _rejected(source, "NO_ANCHOR", source_run, target_run)
        canonical_source = tuple((event.semantic_key, event.occurrence) for event in source_events)
        canonical_target = tuple((event.semantic_key, event.occurrence) for event in target_events)
        if canonical_source == canonical_target and all(_event_context_compatible(source_event, target_event) for source_event, target_event in zip(source_events, target_events)):
            target = _position_from_event(target_events[source_index], target_run, source.requested_icount)
            status = "snapped" if source.actual_delta_instructions not in {None, 0} else "exact"
            matched = Correspondence(source, target, status, evidence={"method": "canonical_occurrence_identity", "source_event_index": source_index, "target_event_index": source_index, "occurrence_transform": target.occurrence - source.occurrence})
            return AlignmentResult(source, matched, "matched", None, top_paths=({"score": None, "matches": len(source_events)},), manifest_hashes=_manifests(source_run, target_run), diagnostics={"method": "canonical_occurrence_identity", "source_events": len(source_events), "target_events": len(target_events)})
        cells = (len(source_events) + 1) * (len(target_events) + 1)
        if cells > int(policy.get("max_cells", 2_000_000)):
            return _rejected(source, "SEARCH_TRUNCATED", source_run, target_run, diagnostics={"cells": cells})
        try:
            paths = _global_paths(
                source_events,
                target_events,
                correspondence,
                gap_penalty=float(policy.get("gap_penalty", -1.0)),
                deadline=time.monotonic() + float(policy.get("max_seconds", 30.0)),
            )
        except _SearchTruncated:
            return _rejected(source, "SEARCH_TRUNCATED", source_run, target_run, diagnostics={"cells": cells})
        if not paths:
            return _rejected(source, "NO_CANDIDATE", source_run, target_run)
        best = paths[0]
        second = paths[1] if len(paths) > 1 else None
        margin = best.score - second.score if second else None
        top_paths = tuple({"score": path.score, "matches": sum(i is not None and j is not None for i, j in path.ops), "target_index": next((j for i, j in path.ops if i == source_index and j is not None), None)} for path in paths)
        best_target_index = next((j for i, j in best.ops if i == source_index and j is not None), None)
        second_target_index = next((j for i, j in second.ops if i == source_index and j is not None), None) if second else best_target_index
        if second and margin is not None and margin <= float(policy.get("tie_epsilon", 1e-12)) and second_target_index != best_target_index:
            return _rejected(source, "AMBIGUOUS", source_run, target_run, margin=margin, top_paths=top_paths, position_status="ambiguous")
        target_index = best_target_index
        if target_index is None:
            return _rejected(source, "NO_CORRESPONDENCE", source_run, target_run, margin=margin, top_paths=top_paths)
        target = _position_from_event(target_events[target_index], target_run, source.requested_icount)
        position_status = "snapped" if source.actual_delta_instructions not in {None, 0} else "exact"
        result = Correspondence(
            source,
            target,
            position_status,
            evidence={
                "method": "global_semantic_event_dp",
                "source_event_index": source_index,
                "target_event_index": target_index,
                "occurrence_transform": target.occurrence - source.occurrence,
            },
        )
        return AlignmentResult(
            source,
            result,
            "matched",
            margin,
            top_paths=top_paths,
            manifest_hashes=_manifests(source_run, target_run),
            diagnostics={"cells": cells, "source_events": len(source_events), "target_events": len(target_events)},
        )


def _trace(value):
    if isinstance(value, Mapping):
        return value.get("run", value.get("manifest")), value.get("events", ()), value
    return value.run, value.events, value


def _dynamic_evidence(value) -> bool:
    if isinstance(value, Mapping):
        return value.get("evidence_kind") == "dynamic_execution" and value.get("complete") is True
    return getattr(value, "evidence_kind", None) == "dynamic_execution" and getattr(value, "complete", False) is True


def _compatible(source: BuildRun, target: BuildRun) -> bool:
    keys = ("workload_id", "input_id", "functional_path_id", "icount_domain", "event_phase")
    return all(getattr(source, key) == getattr(target, key) for key in keys)


def _same_position(event: ProgressEvent, position: Position) -> bool:
    return (event.build_id, event.anchor_id, event.occurrence) == (position.build_id, position.anchor_id, position.occurrence)


def _position_from_event(event: ProgressEvent, run: BuildRun, requested: int | None) -> Position:
    if event.workload_icount is None:
        return Position(run.build_id, event.anchor_id, event.occurrence, event.pc, None, None, None, None, None)
    interval, offset = divmod(event.workload_icount, run.interval_instructions)
    return Position(run.build_id, event.anchor_id, event.occurrence, event.pc, event.workload_icount, interval, offset, event.workload_icount, 0)


def _keep_top(paths):
    unique = {path.ops: path for path in paths}
    def key(path):
        normalized = tuple((i if i is not None else -1, j if j is not None else -1) for i, j in path.ops)
        return -path.score, normalized
    return tuple(sorted(unique.values(), key=key)[:2])


def _global_paths(source, target, correspondence, *, gap_penalty, deadline):
    table = [[() for _ in range(len(target) + 1)] for _ in range(len(source) + 1)]
    table[0][0] = (_Path(0.0, ()),)
    for i in range(len(source) + 1):
        for j in range(len(target) + 1):
            if not table[i][j]:
                continue
            if time.monotonic() > deadline:
                raise _SearchTruncated
            if i < len(source):
                table[i + 1][j] = _keep_top([*table[i + 1][j], *(_Path(path.score + gap_penalty, path.ops + ((i, None),)) for path in table[i][j])])
            if j < len(target):
                table[i][j + 1] = _keep_top([*table[i][j + 1], *(_Path(path.score + gap_penalty, path.ops + ((None, j),)) for path in table[i][j])])
            if i < len(source) and j < len(target):
                evidence = next((item for item in semantic_candidates(source[i], (target[j],), correspondence)), None)
                if evidence:
                    _, score, _ = evidence
                    candidates = list(table[i + 1][j + 1])
                    for path in table[i][j]:
                        candidates.append(_Path(path.score + score, path.ops + ((i, j),)))
                    table[i + 1][j + 1] = _keep_top(candidates)
    return table[-1][-1]


def _manifests(source: BuildRun, target: BuildRun) -> dict[str, str]:
    return {"source": source.manifest_sha256, "target": target.manifest_sha256}


def _event_context_compatible(source: ProgressEvent, target: ProgressEvent) -> bool:
    source_context, target_context = source.context, target.context
    if not isinstance(source_context, Mapping) or not isinstance(target_context, Mapping):
        return True
    return all(key not in target_context or target_context[key] == value for key, value in source_context.items())


def _rejected(source, reason, source_run, target_run, *, margin=None, top_paths=(), position_status="no_correspondence", diagnostics=None):
    correspondence = Correspondence(source, None, position_status, reason=reason)
    return AlignmentResult(source, correspondence, "rejected", margin, top_paths=top_paths, manifest_hashes=_manifests(source_run, target_run), diagnostics=diagnostics or {})
