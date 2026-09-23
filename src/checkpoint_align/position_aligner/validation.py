"""Independent post-alignment validation contracts.

Validation is deliberately strict: absent evidence is never treated as an
equal value.  A report can only be validated when both sides carry a complete
binding for the run and checkpoint being compared.
"""

from __future__ import annotations

from typing import Any, Mapping


def _missing(value: Mapping[str, Any], required: tuple[str, ...]) -> list[str]:
    if not isinstance(value, Mapping):
        return list(required)
    return [name for name in required if name not in value or value[name] is None]


def _insufficient(validation: str, missing: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "validation": validation,
        "status": "insufficient_evidence",
        "reason": "INSUFFICIENT_EVIDENCE",
        "missing_fields": missing,
        **extra,
    }


def _event_identity(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return value.get("anchor_id"), value.get("occurrence")


def validate_restore(source: Mapping[str, Any], restored: Mapping[str, Any]) -> dict[str, Any]:
    required = ("build_id", "run_id", "target_event", "state_digest", "subsequent_events", "terminal_observed", "checkpoint_sha256", "position")
    if not isinstance(source, Mapping) or not isinstance(restored, Mapping):
        return _insufficient("restore", ["source.mapping", "restored.mapping"])
    missing = [f"source.{name}" for name in _missing(source, required)] + [f"restored.{name}" for name in _missing(restored, required)]
    for side, value in (("source", source), ("restored", restored)):
        if not isinstance(value.get("target_event"), Mapping):
            missing.append(f"{side}.target_event.mapping")
        else:
            missing.extend(f"{side}.target_event.{name}" for name in _missing(value["target_event"], ("anchor_id", "occurrence")))
        if not isinstance(value.get("position"), Mapping):
            missing.append(f"{side}.position.mapping")
        else:
            missing.extend(f"{side}.position.{name}" for name in _missing(value["position"], ("anchor_id", "occurrence")))
    if missing:
        return _insufficient("restore", missing)
    for side, value in (("source", source), ("restored", restored)):
        if any(not isinstance(value[name], str) or not value[name] for name in ("build_id", "run_id", "state_digest", "checkpoint_sha256")):
            missing.append(f"{side}.typed_binding")
        if not isinstance(value["subsequent_events"], (list, tuple)) or not isinstance(value["terminal_observed"], bool):
            missing.append(f"{side}.typed_state_evidence")
        if len(value["checkpoint_sha256"]) != 64:
            missing.append(f"{side}.checkpoint_sha256")
        else:
            try:
                int(value["checkpoint_sha256"], 16)
            except ValueError:
                missing.append(f"{side}.checkpoint_sha256")
    if missing:
        return _insufficient("restore", missing)
    checks = {
        "same_build": source["build_id"] == restored["build_id"],
        "same_run": source["run_id"] == restored["run_id"],
        "same_position": source["position"] == restored["position"],
        "same_checkpoint": source["checkpoint_sha256"] == restored["checkpoint_sha256"],
        "event_identity": _event_identity(source["target_event"]) == _event_identity(restored["target_event"]),
        "state_digest": source["state_digest"] == restored["state_digest"],
        "subsequent_events": source["subsequent_events"] == restored["subsequent_events"],
        "terminal_marker": source["terminal_observed"] is True and restored["terminal_observed"] is True,
        "marker_not_consumed": restored.get("marker_consumed") is False,
    }
    if "marker_consumed" not in restored:
        return _insufficient("restore", ["restored.marker_consumed"])
    return {"schema_version": 1, "validation": "restore", "status": "validated" if all(checks.values()) else "failed", "reason": None if all(checks.values()) else "RESTORE_DIVERGENCE", "checks": checks}


def validate_cross_build(source: Mapping[str, Any], target: Mapping[str, Any], position_status: str) -> dict[str, Any]:
    required = ("alignment_id", "build_id", "run_id", "position", "context", "before_marker", "after_marker", "application_state")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return _insufficient("cross_build", ["source.mapping", "target.mapping"], position_status=position_status)
    missing = [f"source.{name}" for name in _missing(source, required)] + [f"target.{name}" for name in _missing(target, required)]
    for side, value in (("source", source), ("target", target)):
        if not isinstance(value.get("context"), Mapping):
            missing.append(f"{side}.context.mapping")
        else:
            missing.extend(f"{side}.context.{name}" for name in _missing(value["context"], ("work_unit", "subphase")))
        if not isinstance(value.get("position"), Mapping):
            missing.append(f"{side}.position.mapping")
        else:
            missing.extend(f"{side}.position.{name}" for name in _missing(value["position"], ("anchor_id", "occurrence")))
    if missing:
        return _insufficient("cross_build", missing, position_status=position_status)
    for side, value in (("source", source), ("target", target)):
        if any(not isinstance(value[name], str) or not value[name] for name in ("alignment_id", "build_id", "run_id")):
            missing.append(f"{side}.typed_binding")
    if missing:
        return _insufficient("cross_build", missing, position_status=position_status)
    source_context = source["context"]
    target_context = target["context"]
    checks = {
        "accepted_position": position_status in {"exact", "snapped"},
        "same_alignment": source["alignment_id"] == target["alignment_id"],
        "work_unit": source_context["work_unit"] == target_context["work_unit"],
        "subphase": source_context["subphase"] == target_context["subphase"],
        "before_marker": source["before_marker"] == target["before_marker"],
        "after_marker": source["after_marker"] == target["after_marker"],
        "application_state": source["application_state"] == target["application_state"],
    }
    status = "validated" if all(checks.values()) else "failed"
    return {"schema_version": 1, "validation": "cross_build", "status": status, "reason": None if status == "validated" else "POST_ALIGN_DIVERGENCE", "position_status": position_status, "checks": checks}


def validate_coverage(full: Mapping[str, Any], sampled: Mapping[str, Any]) -> dict[str, Any]:
    required = ("build_id", "run_id", "phases", "features")
    if not isinstance(full, Mapping) or not isinstance(sampled, Mapping):
        return _insufficient("coverage", ["full.mapping", "sampled.mapping"])
    missing = [f"full.{name}" for name in _missing(full, required)] + [f"sampled.{name}" for name in _missing(sampled, required)]
    if missing:
        return _insufficient("coverage", missing)
    if not isinstance(full["phases"], (list, tuple, set)) or not isinstance(sampled["phases"], (list, tuple, set)) or not isinstance(full["features"], (list, tuple, set)) or not isinstance(sampled["features"], (list, tuple, set)):
        return _insufficient("coverage", ["phases/features must be collections"])
    try:
        full_phases = set(full["phases"])
        sampled_phases = set(sampled["phases"])
        full_features = set(full["features"])
        sampled_features = set(sampled["features"])
    except TypeError:
        return _insufficient("coverage", ["phases/features must contain hashable values"])
    if not full_features:
        return _insufficient("coverage", ["full.features"])
    missed_phases = sorted(full_phases - sampled_phases)
    fraction = len(full_features & sampled_features) / len(full_features)
    same_build = full["build_id"] == sampled["build_id"]
    same_run = full["run_id"] == sampled["run_id"]
    return {
        "schema_version": 1,
        "validation": "coverage",
        "status": "validated" if same_build and same_run and not missed_phases else "failed",
        "reason": None if same_build and same_run and not missed_phases else "COVERAGE_DIVERGENCE",
        "build_id": full["build_id"],
        "same_build": same_build,
        "same_run": same_run,
        "covered_phases": sorted(full_phases & sampled_phases),
        "missed_phases": missed_phases,
        "covered_features": len(full_features & sampled_features),
        "full_features": len(full_features),
        "coverage_fraction": fraction,
        "claim_scope": "B-internal functional behavior coverage only",
    }
