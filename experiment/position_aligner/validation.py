"""Independent post-alignment validation contracts."""

from __future__ import annotations

from typing import Any, Mapping


def _event_identity(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return value.get("anchor_id"), value.get("occurrence"), value.get("event_phase")


def validate_restore(source: Mapping[str, Any], restored: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "same_build": source.get("build_id") == restored.get("build_id"),
        "event_identity": _event_identity(source.get("target_event", {})) == _event_identity(restored.get("target_event", {})),
        "state_digest": source.get("state_digest") == restored.get("state_digest"),
        "subsequent_events": source.get("subsequent_events") == restored.get("subsequent_events"),
        "terminal_marker": bool(source.get("terminal_observed")) and bool(restored.get("terminal_observed")),
        "marker_not_consumed": restored.get("marker_consumed") is False,
    }
    return {"schema_version": 1, "validation": "restore", "status": "validated" if all(checks.values()) else "failed", "checks": checks}


def validate_cross_build(source: Mapping[str, Any], target: Mapping[str, Any], position_status: str) -> dict[str, Any]:
    source_context = source.get("context", {})
    target_context = target.get("context", {})
    checks = {
        "accepted_position": position_status in {"exact", "snapped"},
        "work_unit": source_context.get("work_unit") == target_context.get("work_unit"),
        "subphase": source_context.get("subphase") == target_context.get("subphase"),
        "before_marker": source.get("before_marker") == target.get("before_marker"),
        "after_marker": source.get("after_marker") == target.get("after_marker"),
        "application_state": source.get("application_state") == target.get("application_state"),
    }
    status = "validated" if all(checks.values()) else "failed"
    return {"schema_version": 1, "validation": "cross_build", "status": status, "reason": None if status == "validated" else "POST_ALIGN_DIVERGENCE", "position_status": position_status, "checks": checks}


def validate_coverage(full: Mapping[str, Any], sampled: Mapping[str, Any]) -> dict[str, Any]:
    same_build = full.get("build_id") == sampled.get("build_id")
    full_phases = set(full.get("phases", ()))
    sampled_phases = set(sampled.get("phases", ()))
    full_features = set(full.get("features", ()))
    sampled_features = set(sampled.get("features", ()))
    missed_phases = sorted(full_phases - sampled_phases)
    result = {
        "schema_version": 1,
        "validation": "coverage",
        "status": "validated" if same_build and not missed_phases else "failed",
        "build_id": full.get("build_id"),
        "same_build": same_build,
        "covered_phases": sorted(full_phases & sampled_phases),
        "missed_phases": missed_phases,
        "covered_features": len(full_features & sampled_features),
        "full_features": len(full_features),
        "coverage_fraction": len(full_features & sampled_features) / len(full_features) if full_features else 1.0,
        "claim_scope": "B-internal functional behavior coverage only",
    }
    return result

