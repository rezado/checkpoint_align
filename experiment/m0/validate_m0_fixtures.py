#!/usr/bin/env python3
"""Validate the self-contained M0 protocol and synthetic correspondence gold.

This is intentionally a small stdlib-only checker.  It validates the parts of
the contract that are easy to accidentally change while extending the real
aligner: zero-based interval arithmetic, explicit event phase, typed rejects,
identity exactness, occurrence transforms, and the absence of scoring weight
fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


EVENT_PHASES = {"before_instruction", "after_instruction", "after_taken_edge"}
STATUSES = {"matched", "rejected"}
FIDELITIES = {"exact", "snapped", "interpolated", "rejected"}
CONFIDENCES = {"H", "M", "L", "R"}
VALIDATION_STATES = {"candidate", "validated", "failed"}
POSITION_REASONS = {
    "NO_ANCHOR",
    "NO_CANDIDATE",
    "SEARCH_TRUNCATED",
    "AMBIGUOUS",
    "CROSSING",
    "MARKER_GAP_TOO_LARGE",
    "LOW_FIDELITY",
    "OUT_OF_TRACE",
    "POST_ALIGN_DIVERGENCE",
}
BATCH_REASONS = {
    "INCOMPATIBLE_RUN",
    "NONDETERMINISTIC_TRACE",
    "ARTIFACT_MISMATCH",
    "EVIDENCE_COLLECTION_FAILED",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise ValueError(message)


def require(mapping: Mapping[str, Any], fields: set[str], where: str) -> None:
    missing = sorted(fields - mapping.keys())
    if missing:
        fail(f"{where}: missing {', '.join(missing)}")


def check_type(value: Any, expected: type | tuple[type, ...], where: str) -> None:
    # bool is an int subclass; schema integers must not silently accept it.
    if expected is int and isinstance(value, bool):
        fail(f"{where}: expected integer")
    if not isinstance(value, expected):
        fail(f"{where}: expected {expected}, got {type(value).__name__}")


def reject_weight_keys(value: Any, where: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if "weight" in key.lower():
                fail(f"{where}: forbidden scoring field {key!r}")
            reject_weight_keys(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_weight_keys(child, f"{where}[{index}]")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot load {path}: {exc}")


def validate_hash(value: Any, where: str) -> None:
    check_type(value, str, where)
    if not HEX64.fullmatch(value):
        fail(f"{where}: expected lowercase sha256")


def validate_run(run: Mapping[str, Any], where: str) -> None:
    required = {
        "schema_version",
        "workload_id",
        "build_id",
        "run_id",
        "input_id",
        "functional_path_id",
        "icount_domain",
        "interval_instructions",
        "interval_index_base",
        "event_phase",
        "elf_sha256",
        "manifest_sha256",
        "terminal_marker",
        "deterministic",
    }
    require(run, required, where)
    if run["schema_version"] != 1:
        fail(f"{where}.schema_version: expected 1")
    for key in (
        "workload_id",
        "build_id",
        "run_id",
        "input_id",
        "functional_path_id",
        "terminal_marker",
    ):
        check_type(run[key], str, f"{where}.{key}")
    if run["icount_domain"] != "workload_relative_instructions":
        fail(f"{where}.icount_domain: workload-relative domain required")
    check_type(run["interval_instructions"], int, f"{where}.interval_instructions")
    if run["interval_instructions"] < 1:
        fail(f"{where}.interval_instructions: must be positive")
    if run["interval_index_base"] != 0:
        fail(f"{where}.interval_index_base: must be zero")
    if run["event_phase"] not in EVENT_PHASES:
        fail(f"{where}.event_phase: unknown event phase")
    check_type(run["deterministic"], bool, f"{where}.deterministic")
    validate_hash(run["elf_sha256"], f"{where}.elf_sha256")
    validate_hash(run["manifest_sha256"], f"{where}.manifest_sha256")
    if "event_trace_sha256" in run:
        validate_hash(run["event_trace_sha256"], f"{where}.event_trace_sha256")
    for key in ("terminal_observed",):
        if key in run:
            check_type(run[key], bool, f"{where}.{key}")


def validate_position(
    position: Mapping[str, Any],
    where: str,
    runs: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    required = {
        "schema_version",
        "build_id",
        "run_id",
        "anchor_id",
        "occurrence",
        "event_phase",
        "requested_icount",
        "workload_icount",
        "interval",
        "offset_in_interval",
        "snap_delta_instructions",
    }
    require(position, required, where)
    if position["schema_version"] != 1:
        fail(f"{where}.schema_version: expected 1")
    for key in ("build_id", "run_id", "anchor_id", "event_phase"):
        check_type(position[key], str, f"{where}.{key}")
    if position["event_phase"] not in EVENT_PHASES:
        fail(f"{where}.event_phase: unknown event phase")
    for key in (
        "occurrence",
        "requested_icount",
        "workload_icount",
        "interval",
        "offset_in_interval",
        "snap_delta_instructions",
    ):
        check_type(position[key], int, f"{where}.{key}")
    for key in ("occurrence", "requested_icount", "workload_icount", "interval", "offset_in_interval"):
        if position[key] < 0:
            fail(f"{where}.{key}: must be non-negative")
    if runs is not None:
        run = runs.get(position["run_id"])
        if run is None:
            fail(f"{where}.run_id: unknown run {position['run_id']!r}")
        if position["build_id"] != run["build_id"]:
            fail(f"{where}: build_id does not match run manifest")
        interval_size = run["interval_instructions"]
    else:
        interval_size = 100
    expected_interval, expected_offset = divmod(position["workload_icount"], interval_size)
    if (position["interval"], position["offset_in_interval"]) != (
        expected_interval,
        expected_offset,
    ):
        fail(
            f"{where}: interval projection mismatch; expected "
            f"({expected_interval}, {expected_offset})"
        )
    if position["snap_delta_instructions"] != (
        position["workload_icount"] - position["requested_icount"]
    ):
        fail(f"{where}: snap_delta_instructions must be observed-requested")
    if "context_end_icount" in position:
        check_type(position["context_end_icount"], int, f"{where}.context_end_icount")
        if position["context_end_icount"] < position["requested_icount"]:
            fail(f"{where}.context_end_icount: before requested position")


def validate_correspondence(
    correspondence: Mapping[str, Any],
    where: str,
    runs: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    required = {
        "schema_version",
        "workload_id",
        "source",
        "target",
        "status",
        "anchor_confidence",
        "position_fidelity",
        "validation_state",
        "reason",
        "best_candidates",
        "evidence",
        "label",
        "synthetic",
    }
    require(correspondence, required, where)
    if correspondence["schema_version"] != 1:
        fail(f"{where}.schema_version: expected 1")
    check_type(correspondence["workload_id"], str, f"{where}.workload_id")
    check_type(correspondence["source"], dict, f"{where}.source")
    validate_position(correspondence["source"], f"{where}.source", runs)
    target = correspondence["target"]
    if target is not None:
        check_type(target, dict, f"{where}.target")
        validate_position(target, f"{where}.target", runs)
    if correspondence["status"] not in STATUSES:
        fail(f"{where}.status: unknown status")
    if correspondence["anchor_confidence"] not in CONFIDENCES:
        fail(f"{where}.anchor_confidence: unknown confidence")
    if correspondence["position_fidelity"] not in FIDELITIES:
        fail(f"{where}.position_fidelity: unknown fidelity")
    if correspondence["validation_state"] not in VALIDATION_STATES:
        fail(f"{where}.validation_state: unknown validation state")
    reason = correspondence["reason"]
    if reason is not None and reason not in POSITION_REASONS | BATCH_REASONS:
        fail(f"{where}.reason: unknown typed reason {reason!r}")
    check_type(correspondence["best_candidates"], list, f"{where}.best_candidates")
    check_type(correspondence["evidence"], dict, f"{where}.evidence")
    check_type(correspondence["label"], str, f"{where}.label")
    check_type(correspondence["synthetic"], bool, f"{where}.synthetic")
    if correspondence["status"] == "matched":
        if target is None:
            fail(f"{where}: matched correspondence needs target")
        if reason is not None:
            fail(f"{where}: matched correspondence cannot have reason")
        if correspondence["position_fidelity"] == "rejected":
            fail(f"{where}: matched correspondence cannot be rejected fidelity")
    else:
        if target is not None:
            fail(f"{where}: rejected correspondence cannot force a target")
        if reason is None:
            fail(f"{where}: rejected correspondence needs typed reason")
        if correspondence["position_fidelity"] != "rejected":
            fail(f"{where}: rejected correspondence needs rejected fidelity")
    transform = correspondence.get("occurrence_transform")
    if transform is not None:
        check_type(transform, dict, f"{where}.occurrence_transform")
        if "delta" in transform:
            check_type(transform["delta"], int, f"{where}.occurrence_transform.delta")
            if target is not None:
                actual = target["occurrence"] - correspondence["source"]["occurrence"]
                if transform["delta"] != actual:
                    fail(f"{where}: occurrence transform delta does not match positions")


def validate_schema_fixture(schema: Mapping[str, Any]) -> int:
    if schema.get("schema_version") != 1 or schema.get("synthetic") is not True:
        fail("schema fixture must be version 1 and synthetic")
    contract = schema.get("contract")
    if not isinstance(contract, dict):
        fail("schema fixture contract must be an object")
    if contract.get("interval_index_base") != 0:
        fail("schema fixture must freeze zero-based intervals")
    if contract.get("workload_icount_domain") != "workload_relative_instructions":
        fail("schema fixture must freeze workload-relative icount")
    if set(contract.get("event_phase_values", [])) != EVENT_PHASES:
        fail("schema fixture event phases are incomplete")
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict):
        fail("schema fixture definitions must be an object")
    expected_defs = {
        "BuildRun",
        "SourcePosition",
        "PositionCorrespondence",
        "AlignmentResult",
    }
    if set(definitions) != expected_defs:
        fail("schema fixture must define exactly the four M0 interfaces")
    for name in expected_defs:
        if definitions[name].get("type") != "object":
            fail(f"definition {name} must be an object")
        if not definitions[name].get("required"):
            fail(f"definition {name} has no required fields")
    instances = schema.get("instances")
    if not isinstance(instances, dict) or set(instances) != expected_defs:
        fail("schema fixture instances must cover all four interfaces")
    validate_run(instances["BuildRun"], "schema.instances.BuildRun")
    validate_position(instances["SourcePosition"], "schema.instances.SourcePosition")
    validate_correspondence(
        instances["PositionCorrespondence"], "schema.instances.PositionCorrespondence"
    )
    result = instances["AlignmentResult"]
    require(
        result,
        {
            "schema_version",
            "workload_id",
            "source_build",
            "target_build",
            "source_positions",
            "correspondences",
            "status",
            "anchor_confidence",
            "position_fidelity",
            "validation_state",
            "global_path_margin",
            "evidence",
            "manifest_hashes",
            "reason",
            "label",
            "synthetic",
        },
        "schema.instances.AlignmentResult",
    )
    if result["schema_version"] != 1 or result["status"] not in STATUSES:
        fail("schema.instances.AlignmentResult has invalid envelope")
    if result["synthetic"] is not True:
        fail("schema.instances.AlignmentResult must be synthetic")
    if not isinstance(result["source_positions"], list) or not isinstance(
        result["correspondences"], list
    ):
        fail("schema.instances.AlignmentResult arrays are malformed")
    for i, position in enumerate(result["source_positions"]):
        validate_position(position, f"schema.instances.AlignmentResult.source_positions[{i}]")
    for i, correspondence in enumerate(result["correspondences"]):
        validate_correspondence(
            correspondence, f"schema.instances.AlignmentResult.correspondences[{i}]"
        )
    return len(instances)


def validate_gold(gold: Mapping[str, Any]) -> dict[str, int]:
    if gold.get("schema_version") != 1 or gold.get("synthetic") is not True:
        fail("gold must be version 1 and synthetic")
    contract = gold.get("contract")
    if not isinstance(contract, dict):
        fail("gold contract must be an object")
    if contract.get("interval_index_base") != 0:
        fail("gold must use zero-based intervals")
    if contract.get("workload_icount_domain") != "workload_relative_instructions":
        fail("gold must use workload-relative icount")
    if contract.get("event_phase") != "before_instruction":
        fail("gold event phase must be explicit")
    runs = gold.get("runs")
    if not isinstance(runs, dict) or not runs:
        fail("gold runs must be a non-empty object")
    run_lookup: dict[str, Mapping[str, Any]] = dict(runs)
    for run_id, run in runs.items():
        if not isinstance(run, dict):
            fail(f"gold.runs.{run_id}: expected object")
        validate_run(run, f"gold.runs.{run_id}")
        if run["run_id"] in run_lookup and run_lookup[run["run_id"]] is not run:
            fail(f"gold.runs.{run_id}: duplicate run_id")
        run_lookup[run["run_id"]] = run
    cases = gold.get("cases")
    if not isinstance(cases, list) or not cases:
        fail("gold cases must be a non-empty array")
    case_ids: set[str] = set()
    counts = {
        "identity_exact": 0,
        "controlled_positive": 0,
        "negative_rejected": 0,
        "boundary_evaluated": 0,
    }
    for index, case in enumerate(cases):
        where = f"gold.cases[{index}]"
        if not isinstance(case, dict):
            fail(f"{where}: expected object")
        require(
            case,
            {
                "case_id",
                "kind",
                "label",
                "synthetic",
                "source_run_id",
                "target_run_id",
                "expected_status",
                "correspondences",
            },
            where,
        )
        if case["case_id"] in case_ids:
            fail(f"{where}: duplicate case_id")
        case_ids.add(case["case_id"])
        if case["synthetic"] is not True:
            fail(f"{where}: every case must be marked synthetic")
        source_run = run_lookup.get(case["source_run_id"])
        target_run = run_lookup.get(case["target_run_id"])
        if source_run is None or target_run is None:
            fail(f"{where}: unknown source or target run")
        if case["expected_status"] not in STATUSES:
            fail(f"{where}.expected_status: unknown status")
        correspondences = case["correspondences"]
        if not isinstance(correspondences, list):
            fail(f"{where}.correspondences: expected array")
        for cindex, correspondence in enumerate(correspondences):
            cwhere = f"{where}.correspondences[{cindex}]"
            validate_correspondence(correspondence, cwhere, run_lookup)
            if correspondence["workload_id"] != source_run["workload_id"]:
                fail(f"{cwhere}: source workload mismatch")
            source = correspondence["source"]
            if source["run_id"] != source_run["run_id"]:
                fail(f"{cwhere}: source run mismatch")
            target = correspondence["target"]
            if target is not None and target["run_id"] != target_run["run_id"]:
                fail(f"{cwhere}: target run mismatch")
            if correspondence["status"] != case["expected_status"]:
                fail(f"{cwhere}: status differs from case expected_status")
        if case["kind"] == "identity":
            if source_run["build_id"] != target_run["build_id"]:
                fail(f"{where}: identity builds must be the same")
            if not correspondences:
                fail(f"{where}: identity needs positions")
            for cindex, correspondence in enumerate(correspondences):
                if correspondence["status"] != "matched" or correspondence["position_fidelity"] != "exact":
                    fail(f"{where}.correspondences[{cindex}]: identity is not exact")
                source, target = correspondence["source"], correspondence["target"]
                if target is None:
                    fail(f"{where}.correspondences[{cindex}]: identity target missing")
                for key in ("anchor_id", "occurrence", "event_phase"):
                    if source[key] != target[key]:
                        fail(f"{where}.correspondences[{cindex}]: identity {key} differs")
                counts["identity_exact"] += 1
        if case["kind"] in {"controlled_positive", "controlled_snapped"}:
            if case["expected_status"] != "matched" or not correspondences:
                fail(f"{where}: controlled positive must contain matched positions")
            if case["kind"] == "controlled_positive" and not any(
                c.get("occurrence_transform", {}).get("delta", 0) != 0
                for c in correspondences
            ):
                fail(f"{where}: controlled positive needs a non-zero occurrence transform")
            if case["kind"] == "controlled_snapped" and not any(
                c["position_fidelity"] == "snapped"
                and c["source"].get("snap_delta_instructions", 0) != 0
                for c in correspondences
            ):
                fail(f"{where}: controlled snapped case needs observed snap displacement")
            counts["controlled_positive"] += len(correspondences)
        if case["kind"] in {"negative", "protocol_negative"}:
            if case["expected_status"] != "rejected":
                fail(f"{where}: negative must be rejected")
            if case["kind"] == "negative":
                if case.get("expected_reason") not in POSITION_REASONS:
                    fail(f"{where}: negative needs a position reason")
                if len(correspondences) != 1 or correspondences[0]["reason"] != case["expected_reason"]:
                    fail(f"{where}: negative reason is not represented by correspondence")
            else:
                if case.get("batch_reason") not in BATCH_REASONS:
                    fail(f"{where}: protocol negative needs a batch reason")
                if correspondences:
                    fail(f"{where}: protocol negative must not emit a position result")
            counts["negative_rejected"] += 1
        if case["kind"] == "boundary_perturbation":
            delta = case.get("boundary_delta")
            if delta not in {-1, 1}:
                fail(f"{where}: boundary_delta must be +1 or -1")
            if case["expected_status"] not in STATUSES:
                fail(f"{where}: boundary outcome must be explicit")
            if not correspondences:
                fail(f"{where}: boundary case needs evaluated correspondence")
            counts["boundary_evaluated"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory containing schema_fixture.json and gold-correspondence.json",
    )
    args = parser.parse_args()
    schema_path = args.directory / "schema_fixture.json"
    gold_path = args.directory / "gold-correspondence.json"
    schema = load_json(schema_path)
    gold = load_json(gold_path)
    reject_weight_keys(schema, "schema")
    reject_weight_keys(gold, "gold")
    definitions = validate_schema_fixture(schema)
    counts = validate_gold(gold)
    summary = {
        "schema_definitions": definitions,
        "gold_cases": len(gold["cases"]),
        **counts,
        "sha256": {
            "schema_fixture": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            "gold_correspondence": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        },
        "status": "ok",
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(f"M0 fixture validation failed: {exc}") from exc
