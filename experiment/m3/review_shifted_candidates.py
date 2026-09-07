#!/usr/bin/env python3
"""Re-evaluate legacy non-zero BBV candidates with the M1 position aligner.

The report produced by this module is deliberately a bounded *candidate*
review.  It compares the ratio-only locator, the old local BBV greedy result,
and the M1 global BBV result.  A DWARF/source result is only attempted when an
independent occurrence trace is supplied.  Static DWARF catalogs alone do not
provide dynamic occurrences, so the default report records a typed evidence
collection rejection instead of fabricating a semantic match.

No checkpoint, weight file, simulator, or legacy result is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


# Allow both ``python3 -m experiment.m3.review_shifted_candidates`` and the
# direct path used in the README when the repository is not installed.
try:
    from experiment.position_aligner import AlignmentPolicy, align, build_index
    from experiment.dwarf_source import OccurrenceTrace, align_occurrence_sequences
except ModuleNotFoundError:  # pragma: no cover - only exercised by path launch
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from experiment.position_aligner import AlignmentPolicy, align, build_index
    from experiment.dwarf_source import OccurrenceTrace, align_occurrence_sequences


SCHEMA_VERSION = 1
NO_ANCHOR = "NO_ANCHOR"
AMBIGUOUS = "AMBIGUOUS"
EVIDENCE_COLLECTION_FAILED = "EVIDENCE_COLLECTION_FAILED"
ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
LOW_FIDELITY = "LOW_FIDELITY"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _legacy_candidates(results_root: Path) -> list[dict[str, Any]]:
    """Load only the old accepted ``A index != B index`` candidates."""

    candidates: list[dict[str, Any]] = []
    for path in sorted(results_root.glob("*/alignment.json")):
        workload = path.parent.name
        value = _read_json(path)
        for region in value.get("regions", []):
            source = int(region["source_point_a"])
            best = region.get("best", {})
            target = best.get("target_point_b")
            if target is None or source == int(target):
                continue
            if region.get("status") != "accepted_experimental":
                continue
            radius = int(value.get("search_radius", 0))
            center = int(region.get("ratio_search_center_b", source))
            candidates.append(
                {
                    "candidate_id": f"{workload}:A{source}->B{int(target)}",
                    "workload": workload,
                    "source_interval": source,
                    "legacy_target_interval": int(target),
                    "legacy_source_cluster": region.get("source_cluster_a"),
                    "legacy_status": region.get("status"),
                    "legacy_score": best.get("score"),
                    "legacy_margin": region.get("margin"),
                    "legacy_search_center": center,
                    "legacy_search_radius": radius,
                    "legacy_edge_hit": abs(int(target) - center) == radius,
                    "legacy_file": str(path),
                }
            )
    candidates.sort(key=lambda item: (item["workload"], item["source_interval"]))
    return candidates


def _workload_metadata(suite: Path, workload: str, side: str) -> tuple[Path, dict[str, Any]]:
    base = suite / "workloads" / workload / side
    metadata_path = base / "json" / f"{workload}.json"
    return base, _read_json(metadata_path)[workload]


def _run_spec(
    suite: Path,
    suite_manifest: Mapping[str, Any],
    suite_manifest_hash: str,
    workload: str,
    side: str,
    interval: int,
) -> dict[str, Any]:
    base, metadata = _workload_metadata(suite, workload, side)
    run_script = base / "cmd" / f"{workload}.run.sh"
    copied = suite_manifest.get("copied", {}).get(workload, {}).get(side, {})
    elf_sha256 = copied.get("elf", {}).get("sha256")
    # The run script is identical for the two sides in this suite and is the
    # only input identity available in the prepared artifact.  ELF hashes are
    # retained as provenance, not as a compatibility key.
    input_fingerprint = _sha256(run_script) if run_script.is_file() else None
    manifest = {
        "schema_version": 1,
        "workload_id": workload,
        "build_id": side,
        "run_id": f"{workload}-{side}-m3-review",
        "icount_domain": "workload_relative_instructions",
        "interval_instructions": interval,
        "interval_index_base": 0,
        "event_phase": "before_instruction",
        "input_fingerprint": input_fingerprint,
        "functional_path": f"{workload}:prepared-suite-default",
        "suite_manifest_sha256": suite_manifest_hash,
        "elf_sha256": elf_sha256,
        "artifact_manifest": copied,
    }
    return {
        "build_id": side,
        "run_id": manifest["run_id"],
        "bbv_path": base / "profiling" / "simpoint_bbv.gz",
        "interval_instructions": interval,
        "total_instructions": int(metadata["insts"]),
        "workload_id": workload,
        "manifest": manifest,
        "manifest_hash": _hash_json(manifest),
        "input_fingerprint": input_fingerprint,
        "functional_path": manifest["functional_path"],
    }


def _target_interval(position: Mapping[str, Any] | None) -> int | None:
    if not position:
        return None
    target = position.get("target")
    if not isinstance(target, Mapping):
        return None
    nested = target.get("position")
    if not isinstance(nested, Mapping):
        return None
    value = nested.get("interval", nested.get("occurrence"))
    return int(value) if value is not None else None


def _ratio_method(candidate: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    source = int(candidate["source_interval"])
    center = int(round(source * ratio))
    return {
        "status": "rejected",
        "reason": NO_ANCHOR,
        "anchor_confidence": "R",
        "position_fidelity": "rejected",
        "validation_state": "candidate",
        "target_interval": center,
        "label": "ratio_only_diagnostic",
        "semantic_anchor_trace_present": False,
        "evidence": {"ratio": ratio, "ratio_center_interval": center},
    }


def _legacy_method(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "matched",
        "reason": None,
        "anchor_confidence": "L",
        "position_fidelity": "interpolated",
        "validation_state": "candidate",
        "target_interval": int(candidate["legacy_target_interval"]),
        "label": "legacy_bbv_greedy",
        "semantic_anchor_trace_present": False,
        "production_eligible": False,
        "legacy_status": candidate["legacy_status"],
        "score": candidate["legacy_score"],
        "margin": candidate["legacy_margin"],
        "search_center_interval": candidate["legacy_search_center"],
        "search_radius": candidate["legacy_search_radius"],
        "edge_hit": candidate["legacy_edge_hit"],
    }


def _m1_position_method(
    result: Mapping[str, Any],
    *,
    sequence_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    positions = result.get("positions", [])
    position = positions[0] if positions and isinstance(positions[0], Mapping) else {}
    evidence = position.get("evidence", {}) if isinstance(position, Mapping) else {}
    search = result.get("search", [])
    search_row = search[0] if search and isinstance(search[0], Mapping) else {}
    target = _target_interval(position)
    status = str(result.get("status", "rejected"))
    reason = result.get("reason")
    if reason is None and isinstance(position, Mapping):
        reason = position.get("reason")
    return {
        "status": status,
        "reason": reason,
        "candidate_status": position.get("status") if isinstance(position, Mapping) else None,
        "anchor_confidence": result.get("anchor_confidence", "R"),
        "position_fidelity": result.get("position_fidelity", "rejected"),
        "validation_state": result.get("validation_state", "candidate"),
        "target_interval": target,
        "label": "bbv_global_m1",
        "semantic_anchor_trace_present": False,
        "production_eligible": bool(result.get("production_eligible", False)),
        "score": evidence.get("score"),
        "global_path_score": result.get("global_path_score"),
        "global_path_margin": result.get("global_path_margin"),
        "top_two_candidates": [
            {"target_interval": item.get("target_point_b"), "score": item.get("score")}
            for item in position.get("candidates", [])[:2]
            if isinstance(item, Mapping)
        ]
        if isinstance(position, Mapping)
        else [],
        "search": {
            "center": search_row.get("center"),
            "radius": search_row.get("radius"),
            "expansions": search_row.get("expansions"),
            "stable": search_row.get("stable"),
            "truncation_reason": search_row.get("truncation_reason"),
        },
        "sequence_batch": (
            {
                "status": sequence_result.get("status"),
                "reason": sequence_result.get("reason"),
                "global_path_score": sequence_result.get("global_path_score"),
                "global_path_margin": sequence_result.get("global_path_margin"),
                "mapped_position_count": sequence_result.get("mapped_position_count"),
                "source_gap_count": sequence_result.get("source_gap_count"),
                "target_gap_count": sequence_result.get("target_gap_count"),
            }
            if sequence_result is not None
            else None
        ),
    }


def _load_trace_pair(trace_manifest: Mapping[str, Any], workload: str) -> tuple[OccurrenceTrace, OccurrenceTrace] | None:
    value = trace_manifest.get(workload)
    if not isinstance(value, Mapping):
        return None
    source_path = value.get("A", value.get("source"))
    target_path = value.get("B", value.get("target"))
    if not source_path or not target_path:
        return None
    source = Path(source_path)
    target = Path(target_path)
    if not source.is_file() or not target.is_file():
        return None
    return OccurrenceTrace.load(source), OccurrenceTrace.load(target)


def _dwarf_method(
    candidate: Mapping[str, Any],
    *,
    catalog_available: bool,
    catalog_manifest_match: bool,
    catalog_manifest_hash: str | None,
    suite_manifest_hash: str,
    trace_pair: tuple[OccurrenceTrace, OccurrenceTrace] | None,
    expected_build_identities: tuple[str | None, str | None],
) -> dict[str, Any]:
    base = {
        "status": "rejected",
        "reason": EVIDENCE_COLLECTION_FAILED,
        "anchor_confidence": "R",
        "position_fidelity": "rejected",
        "validation_state": "candidate",
        "target_interval": None,
        "label": "dwarf_source_global",
        "production_eligible": False,
        "catalog_available": catalog_available,
        "catalog_manifest_match": catalog_manifest_match,
        "catalog_manifest_sha256": catalog_manifest_hash,
        "suite_manifest_sha256": suite_manifest_hash,
        "independent_occurrence_trace_present": trace_pair is not None,
    }
    if trace_pair is None:
        base["evidence"] = {
            "reason_detail": "No independent dynamic occurrence trace was supplied; static DWARF catalog is insufficient.",
            "source_interval": candidate["source_interval"],
            "catalog_binding": (
                "current_suite_manifest"
                if catalog_manifest_match
                else "catalog_manifest_mismatch_or_missing"
            ),
        }
        return base

    source_trace, target_trace = trace_pair
    expected_source, expected_target = expected_build_identities
    if (
        (expected_source is not None and source_trace.build_identity != expected_source)
        or (expected_target is not None and target_trace.build_identity != expected_target)
    ):
        base["reason"] = ARTIFACT_MISMATCH
        base["evidence"] = {
            "expected_build_identities": {"source": expected_source, "target": expected_target},
            "observed_build_identities": {
                "source": source_trace.build_identity,
                "target": target_trace.build_identity,
            },
        }
        return base
    alignment = align_occurrence_sequences(
        source_trace.events,
        target_trace.events,
        source_build=source_trace.build_identity,
        target_build=target_trace.build_identity,
    )
    source_icount = int(candidate["source_interval"]) * 20_000_000
    source_events = [
        (index, event)
        for index, event in enumerate(source_trace.events)
        if event.workload_icount is not None
    ]
    if not source_events:
        base["evidence"] = {"sequence_alignment": alignment.to_dict(), "reason_detail": "Trace has no workload icounts."}
        return base
    source_index, source_event = min(source_events, key=lambda item: abs(int(item[1].workload_icount) - source_icount))
    snap_delta = int(source_event.workload_icount) - source_icount
    target_index = next((match.target_index for match in alignment.matches if match.source_index == source_index), None)
    if target_index is None or target_index >= len(target_trace.events):
        base["reason"] = alignment.reason or EVIDENCE_COLLECTION_FAILED
        base["evidence"] = {"sequence_alignment": alignment.to_dict(), "source_event_index": source_index}
        return base
    target_event = target_trace.events[target_index]
    exact = snap_delta == 0
    base.update(
        {
            "status": "matched" if alignment.status == "matched" and exact else "rejected",
            "reason": alignment.reason if alignment.status != "matched" else (None if exact else LOW_FIDELITY),
            "anchor_confidence": "M" if alignment.status == "matched" else "R",
            "position_fidelity": "exact" if alignment.status == "matched" and exact else ("snapped" if alignment.status == "matched" else "rejected"),
            "target_anchor_id": target_event.anchor_id,
            "target_occurrence": target_event.occurrence,
            "target_event_phase": target_event.event_phase,
            "evidence": {
                "sequence_alignment": alignment.to_dict(),
                "source_event_index": source_index,
                "requested_source_icount": source_icount,
                "source_event_icount": source_event.workload_icount,
                "snap_delta_instructions": snap_delta,
            },
        }
    )
    return base


def _sequence_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "global_path_score": result.get("global_path_score"),
        "global_path_margin": result.get("global_path_margin"),
        "mapped_position_count": result.get("mapped_position_count"),
        "source_gap_count": result.get("source_gap_count"),
        "target_gap_count": result.get("target_gap_count"),
        "search": result.get("search", []),
    }


def review(
    suite: Path,
    *,
    max_window: int = 128,
    max_seconds: float = 30.0,
    min_score: float = 0.55,
    min_global_margin: float = 0.10,
    trace_manifest_path: Path | None = None,
) -> dict[str, Any]:
    suite = suite.resolve()
    suite_manifest_path = suite / "suite-manifest.json"
    suite_manifest = _read_json(suite_manifest_path)
    suite_manifest_hash = _sha256(suite_manifest_path)
    results_root = suite / "results"
    candidates = _legacy_candidates(results_root)
    if not candidates:
        raise ValueError(f"no accepted non-zero candidates in {results_root}")

    trace_manifest: Mapping[str, Any] = {}
    if trace_manifest_path is not None:
        loaded = _read_json(trace_manifest_path)
        trace_manifest = loaded.get("workloads", loaded) if isinstance(loaded, Mapping) else {}
    catalog_path = suite.parent / "dwarf-source" / "catalog-summary.json"
    if not catalog_path.is_file():
        catalog_path = Path("experiment/dwarf-source/catalog-summary.json")
    catalog_summary = _read_json(catalog_path) if catalog_path.is_file() else {}
    catalog_entries = catalog_summary.get("workloads", {})
    if not isinstance(catalog_entries, Mapping):
        catalog_entries = {}
    catalog_workloads = {
        str(workload)
        for workload, item in catalog_entries.items()
        if isinstance(item, Mapping)
        and item.get("A", {}).get("status") == "ok"
        and item.get("B", {}).get("status") == "ok"
    }
    catalog_manifest_hash = None
    manifest_record = catalog_summary.get("manifest")
    if isinstance(manifest_record, Mapping):
        value = manifest_record.get("sha256")
        catalog_manifest_hash = str(value) if value else None
    catalog_manifest_match = catalog_manifest_hash == suite_manifest_hash

    policy = AlignmentPolicy(
        max_window=max_window,
        max_seconds=max_seconds,
        min_score=min_score,
        min_global_margin=min_global_margin,
    )
    by_workload: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_workload.setdefault(candidate["workload"], []).append(candidate)

    rendered_candidates: list[dict[str, Any]] = []
    sequence_reports: dict[str, Any] = {}
    for workload, workload_candidates in sorted(by_workload.items()):
        interval = int(suite_manifest.get("interval_instructions", 20_000_000))
        runs = [
            _run_spec(suite, suite_manifest, suite_manifest_hash, workload, side, interval)
            for side in ("A", "B")
        ]
        index = build_index(
            runs,
            workload={"workload_id": workload, "suite_manifest_sha256": suite_manifest_hash},
            policy=policy,
        )
        points = [int(item["source_interval"]) for item in workload_candidates]
        sequence_result = align(index, "A", ["B"], points)["results"]["B"]
        sequence_reports[workload] = {
            "query_points": points,
            "result": _sequence_summary(sequence_result),
        }
        ratio = runs[1]["total_instructions"] / runs[0]["total_instructions"]
        trace_pair = _load_trace_pair(trace_manifest, workload)
        catalog_entry = catalog_entries.get(workload, {})
        expected_build_identities = (
            catalog_entry.get("A", {}).get("build_identity") if isinstance(catalog_entry, Mapping) else None,
            catalog_entry.get("B", {}).get("build_identity") if isinstance(catalog_entry, Mapping) else None,
        )
        for candidate in workload_candidates:
            source = int(candidate["source_interval"])
            local_result = align(index, "A", ["B"], [source])["results"]["B"]
            old = dict(candidate)
            old["legacy_file"] = _relative(Path(old["legacy_file"]), suite.parent.parent)
            old["methods"] = {
                "ratio_only": _ratio_method(candidate, ratio),
                "bbv_greedy": _legacy_method(candidate),
                "bbv_global": _m1_position_method(local_result, sequence_result=sequence_result),
                "dwarf_source_global": _dwarf_method(
                    candidate,
                    catalog_available=workload in catalog_workloads,
                    catalog_manifest_match=catalog_manifest_match,
                    catalog_manifest_hash=catalog_manifest_hash,
                    suite_manifest_hash=suite_manifest_hash,
                    trace_pair=trace_pair,
                    expected_build_identities=expected_build_identities,
                ),
            }
            old["artifact_provenance"] = {
                "suite_manifest_sha256": suite_manifest_hash,
                "source_elf_sha256": runs[0]["manifest"].get("elf_sha256"),
                "target_elf_sha256": runs[1]["manifest"].get("elf_sha256"),
                "source_run_manifest_sha256": runs[0]["manifest_hash"],
                "target_run_manifest_sha256": runs[1]["manifest_hash"],
                "source_bbv": _relative(Path(runs[0]["bbv_path"]), suite.parent.parent),
                "target_bbv": _relative(Path(runs[1]["bbv_path"]), suite.parent.parent),
            }
            rendered_candidates.append(old)

    method_summary: dict[str, Any] = {}
    for method in ("ratio_only", "bbv_greedy", "bbv_global", "dwarf_source_global"):
        rows = [item["methods"][method] for item in rendered_candidates]
        status_counts = Counter(str(row.get("status")) for row in rows)
        reason_counts = Counter(str(row.get("reason")) for row in rows if row.get("reason"))
        method_summary[method] = {
            "candidate_count": len(rows),
            "status_counts": dict(sorted(status_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
            "target_interval_agreement_with_legacy": sum(
                row.get("target_interval") == item["legacy_target_interval"]
                for row, item in zip(rows, rendered_candidates)
                if row.get("target_interval") is not None
            ),
        }
    edge_rows = [item for item in rendered_candidates if item["legacy_edge_hit"]]
    edge_stable = [
        item
        for item in edge_rows
        if item["methods"]["bbv_global"].get("search", {}).get("stable") is True
        and item["methods"]["bbv_global"].get("target_interval") == item["legacy_target_interval"]
    ]
    global_sequence_statuses = Counter(item["result"].get("status") for item in sequence_reports.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "m3-shifted-candidate-review",
        "method_scope": {
            "candidate_source": "legacy alignment.json accepted_experimental regions with A index != B index",
            "candidate_count": len(rendered_candidates),
            "workloads": sorted(by_workload),
            "policy": policy.as_dict(),
            "weights_used": False,
            "checkpoint_materializer_invoked": False,
        },
        "inputs": {
            "suite": _relative(suite, suite.parent.parent),
            "suite_manifest": _relative(suite_manifest_path, suite.parent.parent),
            "suite_manifest_sha256": suite_manifest_hash,
            "legacy_results": _relative(results_root, suite.parent.parent),
            "catalog_summary": _relative(catalog_path, suite.parent.parent) if catalog_path.is_file() else None,
            "catalog_summary_sha256": _sha256(catalog_path) if catalog_path.is_file() else None,
            "trace_manifest": _relative(trace_manifest_path, suite.parent.parent) if trace_manifest_path else None,
        },
        "candidates": rendered_candidates,
        "sequence_reports": sequence_reports,
        "method_summary": method_summary,
        "search_window_recheck": {
            "legacy_edge_definition": "abs(target_interval - ratio_search_center) == legacy_search_radius",
            "legacy_edge_count": len(edge_rows),
            "rechecked_count": len(edge_rows),
            "stable_same_target_count": len(edge_stable),
            "stable_same_target_candidates": [item["candidate_id"] for item in edge_stable],
            "global_sequence_status_counts": dict(sorted(global_sequence_statuses.items())),
        },
        "runtime_validation": {
            "status": "not_run",
            "reason": EVIDENCE_COLLECTION_FAILED,
            "validation_state": "candidate",
            "independent_marker_trace_present": bool(trace_manifest),
            "checkpoint_materialized": False,
            "terminal_marker_observed": False,
            "hit_good_trap_observed": False,
            "validated_shifted_pair_count": 0,
            "production_eligible": False,
            "explanation": (
                "The prepared suite has static BBV/DWARF artifacts but no independent dynamic occurrence trace "
                "for these shifted candidates. No B-native checkpoint was launched; BBV agreement remains an "
                "experimental candidate and cannot self-validate semantic position."
            ),
        },
        "status": "candidate_only",
        "production_eligible": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("experiment/multi-workload"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-manifest", type=Path)
    parser.add_argument("--max-window", type=int, default=128)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--min-score", type=float, default=0.55)
    parser.add_argument("--min-global-margin", type=float, default=0.10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = review(
        args.suite,
        max_window=args.max_window,
        max_seconds=args.max_seconds,
        min_score=args.min_score,
        min_global_margin=args.min_global_margin,
        trace_manifest_path=args.trace_manifest,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
