#!/usr/bin/env python3
"""Thin dynamic-progress orchestration entry point.

The subcommands deliberately keep collection, binding, alignment,
materialization, and validation as separate artifacts.  This module is the
single executable entry point for the dynamic occurrence alignment workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from checkpoint_align.dwarf_source import AnchorCatalog, OccurrenceTrace, build_catalog
from checkpoint_align.position_aligner import (
    BuildRun,
    PositionAligner,
    ProgressEvent,
)
from checkpoint_align.position_aligner.materialize import materialization_fingerprint, run_nemu_target, run_nemu_targets, write_target
from checkpoint_align.position_aligner.source_boundary import BoundaryPolicy, CheckpointPoint, NemuSourceProbeRunner, SourceBoundaryResolver
from checkpoint_align.position_aligner.validation import validate_coverage, validate_cross_build, validate_restore


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_from_args(args: argparse.Namespace, elf: Path, manifest_path: Path) -> BuildRun:
    source = _read(manifest_path) if manifest_path.is_file() else {}
    fields = {
        "workload_id": args.workload_id or source.get("workload_id", "controlled"),
        "build_id": args.build_id or source.get("build_id", "B"),
        "run_id": args.run_id or source.get("run_id", f"{args.workload_id or 'controlled'}-{args.build_id or 'B'}-run1"),
        "input_id": args.input_id or source.get("input_id", "input-01"),
        "functional_path_id": args.functional_path_id or source.get("functional_path_id", "default"),
        "icount_domain": source.get("icount_domain", "workload_relative_instructions"),
        "interval_instructions": int(source.get("interval_instructions", args.interval_instructions)),
        "interval_index_base": int(source.get("interval_index_base", 0)),
        "event_phase": source.get("event_phase", "before_instruction"),
        "elf_sha256": _sha256(elf),
        "manifest_sha256": _sha256(manifest_path),
        "terminal_marker": args.terminal_marker or source.get("terminal_marker", "HIT GOOD TRAP"),
        "deterministic": bool(source.get("deterministic", True)),
        "compiler": source.get("compiler", {}),
        "environment": source.get("environment", {}),
        "arguments": tuple(source.get("arguments", source.get("args", ()))),
        "artifacts": source.get("artifacts", {}),
        "tools": source.get("tools", {}),
        "stdout_sha256": source.get("stdout_sha256"),
        "stderr_sha256": source.get("stderr_sha256"),
        "exit_status": source.get("exit_status", 0),
        "elf_type": source.get("elf_type", "ET_EXEC"),
        "thread_count": int(source.get("thread_count", 1)),
    }
    return BuildRun(**fields)


def _load_run_manifest(path: Path) -> BuildRun:
    value = _read(path)
    if "manifest_sha256" not in value:
        value["manifest_sha256"] = value.get("input_manifest_sha256", _sha256(path))
    value.setdefault("workload_id", "unknown")
    value.setdefault("build_id", value.get("elf_sha256", "unknown"))
    value.setdefault("run_id", f"{value['workload_id']}-{value['build_id']}-run1")
    value.setdefault("input_id", value.get("input_manifest_sha256", "unknown"))
    value.setdefault("functional_path_id", "default")
    value.setdefault("icount_domain", "workload_relative_instructions")
    value.setdefault("interval_instructions", 20_000_000)
    value.setdefault("interval_index_base", 0)
    value.setdefault("event_phase", "before_instruction")
    if not value.get("terminal_marker"):
        value["terminal_marker"] = "HIT GOOD TRAP"
    value.setdefault("deterministic", bool(value.get("normalized_trace_stable", True)))
    value.setdefault("elf_sha256", value.get("elf_sha256", "0" * 64))
    value.setdefault("elf_type", "ET_EXEC")
    value.setdefault("thread_count", 1)
    value.setdefault("arguments", tuple(value.get("args", ())))
    value.setdefault("artifacts", {})
    value.setdefault("tools", {"qemu": {"sha256": value.get("qemu_sha256")}, "plugin": {"sha256": value.get("plugin_sha256")}})
    allowed = set(BuildRun.__dataclass_fields__)
    return BuildRun.from_dict({"schema_version": value.get("schema_version", 1), **{key: item for key, item in value.items() if key in allowed}})


def command_freeze(args: argparse.Namespace) -> int:
    elf = args.elf.resolve()
    manifest = args.manifest.resolve()
    run = _manifest_from_args(args, elf, manifest)
    _write(args.output, run.to_dict())
    return 0


def command_collect_events(args: argparse.Namespace) -> int:
    """Collect or normalize a trace while preserving the runner manifest."""

    run = _load_run_manifest(args.run_manifest)
    trace = OccurrenceTrace.load(args.trace)
    expected_identity = {run.build_id, f"sha256:{run.elf_sha256}", run.elf_sha256}
    if trace.build_identity not in expected_identity or trace.unmatched_samples or trace.event_phase != run.event_phase:
        raise ValueError("occurrence trace does not match BuildRun or is incomplete")
    trace_hash = _sha256(args.trace)
    if run.event_trace_sha256 is not None and run.event_trace_sha256 != trace_hash:
        raise ValueError("occurrence trace hash does not match BuildRun")
    raw = _read(args.run_manifest)
    terminal_marker = args.terminal_marker or raw.get("terminal_marker")
    terminal_complete = raw.get("terminal_observed") is True
    runs = raw.get("runs")
    if not isinstance(runs, list) or not runs:
        runs = []
    if terminal_marker and not terminal_complete and runs:
        run_dir = args.run_manifest.parent
        terminal_complete = all(
            terminal_marker in (run_dir / f"stdout-{record['run_index']}.log").read_text(encoding="utf-8", errors="replace")
            or terminal_marker in (run_dir / f"stderr-{record['run_index']}.log").read_text(encoding="utf-8", errors="replace")
            for record in runs
        )
    trace_hash_match = (run.event_trace_sha256 == trace_hash) if run.event_trace_sha256 is not None else all(record.get("occurrences_sha256") == trace_hash for record in runs)
    plugin_complete = bool(runs) and trace_hash_match and terminal_complete and all(record.get("exit_status") == 0 and record.get("plugin_status", {}).get("vcpus") == 1 and not record.get("plugin_status", {}).get("budget_exceeded") and not record.get("plugin_status", {}).get("write_error") and not record.get("plugin_status", {}).get("close_error") and not record.get("plugin_status", {}).get("truncated_watch_ids") for record in runs)
    events = []
    for event in trace.events:
        context = {}
        if getattr(event, "context", ()):
            context["trace_context"] = list(event.context)
        events.append(ProgressEvent(run.build_id, run.run_id, event.anchor_id, event.semantic_key or event.anchor_id, event.occurrence, event.pc or 0, event.workload_icount, context).to_dict())
    _write(args.output, {"schema_version": 1, "evidence_kind": "dynamic_execution", "complete": plugin_complete, "run": run.to_dict(), "events": events, "source_trace_sha256": trace_hash})
    return 0


def bind_source_position(events: list[dict[str, Any]], requested: dict[str, Any]) -> dict[str, Any]:
    if "event_phase" in requested:
        raise ValueError("event_phase belongs to the run manifest, not a source request")
    matches: list[dict[str, Any]] = []
    window = requested.get("window_events")
    if window:
        def identity(event: dict[str, Any]) -> tuple[Any, ...]:
            return event.get("semantic_key"), event.get("pc")
        signature = [identity(event) for event in window]
        starts = [index for index in range(len(events) - len(signature) + 1) if [identity(event) for event in events[index:index + len(signature)]] == signature]
        offset = int(requested.get("source_event_offset", len(signature) // 2))
        if offset < 0 or offset >= len(signature):
            raise ValueError("source_event_offset is outside window_events")
        matches = [events[start + offset] for start in starts]
    elif requested.get("occurrence_scope") == "global_from_scratch":
        matches = [event for event in events if event.get("anchor_id") == requested.get("anchor_id") and event.get("occurrence") == requested.get("occurrence")]
    else:
        return {"schema_version": 1, "status": "rejected", "source_position": requested, "event": None, "reason": "EVIDENCE_COLLECTION_FAILED"}

    if len(matches) != 1:
        return {"schema_version": 1, "status": "rejected", "source_position": requested, "event": None, "reason": "EVIDENCE_COLLECTION_FAILED" if not matches else "AMBIGUOUS"}
    event = matches[0]
    observed_icount = requested.get("observed_workload_icount", event.get("workload_icount"))
    observed_pc = int(requested.get("observed_pc", event["pc"]))
    requested_icount = int(requested.get("requested_icount", observed_icount or 0))
    interval_size = int(requested.get("interval_instructions", 20_000_000))
    icount = int(observed_icount) if observed_icount is not None else None
    source_position = {"build_id": event["build_id"], "anchor_id": event["anchor_id"], "occurrence": event["occurrence"], "pc": observed_pc, "workload_icount": icount, "interval": (icount // interval_size) if icount is not None else None, "offset": (icount % interval_size) if icount is not None else None, "requested_icount": requested_icount if icount is not None else None, "actual_delta_instructions": (icount - requested_icount) if icount is not None else None}
    status = "snapped" if source_position["actual_delta_instructions"] not in {None, 0} else "exact"
    return {"schema_version": 1, "status": status, "source_position": source_position, "event": event, "reason": None}


def command_bind_source(args: argparse.Namespace) -> int:
    source = _read(args.events)
    events = source.get("events", [])
    requested = _read(args.request)
    if requested.get("mode") == "trace-window" and requested.get("events"):
        if args.requested_icount is None:
            raise ValueError("NEMU trace-window binding requires --requested-icount")
        selected = requested["events"][args.source_event_index]
        if args.watchlist is None:
            raise ValueError("NEMU trace-window binding requires --watchlist")
        watchlist = _read(args.watchlist)["watches"]
        watch = watchlist[str(selected["watch_id"])]
        requested = {
            "anchor_id": watch["anchor_id"],
            "semantic_key": watch["semantic_key"],
            "occurrence": selected["occurrence"],
            "occurrence_scope": "global_from_scratch" if requested.get("icount_origin") == 0 else "window_local",
            "requested_icount": args.requested_icount,
            "observed_workload_icount": selected["workload_icount"],
            "observed_pc": selected["pc"],
            "interval_instructions": requested["interval_size"],
        }
    result = bind_source_position(events, requested)
    _write(args.output, result)
    return 0 if result["status"] in {"exact", "snapped"} else 2


def command_align_progress(args: argparse.Namespace) -> int:
    source, target = _read(args.source_events), _read(args.target_events)
    request = _read(args.source_position)
    request = request.get("source_position", request)
    result = PositionAligner().align(source, target, _read(args.correspondence) if args.correspondence else {}, request, policy={"max_cells": args.max_cells, "max_seconds": args.max_seconds})
    _write(args.output, result.to_dict())
    return 0 if result.status == "matched" else 2


def command_materialize(args: argparse.Namespace) -> int:
    result = _read(args.alignment)
    if result.get("status") != "matched":
        raise ValueError("only a matched alignment can be materialized")
    target = result.get("correspondence", {}).get("target")
    if not target:
        raise ValueError("alignment has no B target to materialize")
    run_document = _read(args.run_manifest)
    run = run_document.get("run", run_document)
    if not _bound_semantic_validation(result.get("semantic_validation"), result.get("source", {}), target, run):
        raise ValueError("alignment requires bound validated semantic evidence before materialization")
    target_path = write_target(args.target, target, run_manifest=run)
    if not args.command:
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    materialized = run_nemu_target(target_path, command, run_manifest=run, output_dir=args.output_dir)
    if materialized["exit_status"] != 0:
        return materialized["exit_status"]
    return 0 if materialized["checkpoint_sidecar"] else 2


def _batch_result(items: list[dict[str, Any]], *, plan_only: bool) -> dict[str, Any]:
    counts = {status: sum(item["status"] == status for item in items) for status in ("aligned", "candidate", "materialized", "reused", "rejected", "failed")}
    counts["semantic_validated"] = sum(item.get("semantic_validation") == "validated" for item in items)
    counts["restore_validated"] = sum(item.get("restore_status") == "validated" for item in items)
    complete = counts["candidate"] == 0 and counts["rejected"] == 0 and counts["failed"] == 0
    return {
        "schema_version": 1,
        "status": ("planned" if plan_only else "complete") if complete else "partial",
        "counts": {"requested": len(items), **counts},
        "items": items,
    }


def _bound_semantic_validation(value: Any, source_position: dict[str, Any], target_position: dict[str, Any], run_manifest: dict[str, Any]) -> bool:
    if not isinstance(value, dict) or value.get("status") != "validated":
        return False
    required = {"alignment_id", "source_run_id", "target_run_id", "source_event", "target_event", "source_context", "target_context", "evidence_sha256"}
    if not required.issubset(value) or value.get("target_run_id") != run_manifest.get("run_id"):
        return False
    if not isinstance(value["source_event"], dict) or not isinstance(value["target_event"], dict):
        return False
    if not isinstance(value["source_context"], dict) or not isinstance(value["target_context"], dict):
        return False
    if any(not isinstance(value[name], str) or not value[name] for name in ("alignment_id", "source_run_id", "target_run_id")):
        return False
    evidence_hash = value["evidence_sha256"]
    if not isinstance(evidence_hash, str) or len(evidence_hash) != 64:
        return False
    try:
        int(evidence_hash, 16)
    except ValueError:
        return False
    source_identity = (source_position.get("anchor_id"), source_position.get("occurrence"))
    target_identity = (target_position.get("anchor_id"), target_position.get("occurrence"))
    if (value["source_event"].get("anchor_id"), value["source_event"].get("occurrence")) != source_identity:
        return False
    if (value["target_event"].get("anchor_id"), value["target_event"].get("occurrence")) != target_identity:
        return False
    return all(value["source_context"].get(key) == value["target_context"].get(key) for key in ("work_unit", "subphase"))


def _resume_sidecar(output_dir: Path, target: dict[str, Any], run_manifest: dict[str, Any], command: list[str]) -> Path | None:
    sidecar_path = output_dir / "checkpoint-sidecar.json"
    if not sidecar_path.is_file():
        return None
    sidecar = _read(sidecar_path)
    if "event_phase" in sidecar:
        raise ValueError("existing checkpoint sidecar uses obsolete per-position event_phase")
    for key in ("build_id", "anchor_id", "occurrence", "pc"):
        if sidecar.get(key) != target.get(key):
            raise ValueError(f"existing checkpoint sidecar does not match bound field {key}")
    expected_binding = {"run_id": run_manifest.get("run_id"), "run_manifest_sha256": run_manifest.get("manifest_sha256")}
    for key, expected in expected_binding.items():
        if sidecar.get(key) != expected:
            raise ValueError(f"existing checkpoint sidecar does not match bound field {key}")
    expected_fingerprint = materialization_fingerprint(run_manifest, command, target)
    if sidecar.get("run_fingerprint") != expected_fingerprint:
        raise ValueError("existing checkpoint sidecar does not match materialization fingerprint")
    checkpoint = Path(sidecar["checkpoint"])
    if not checkpoint.is_file():
        raise ValueError("existing checkpoint sidecar points to a missing checkpoint")
    recorded_hash = sidecar.get("checkpoint_sha256")
    if not recorded_hash:
        raise ValueError("existing checkpoint sidecar is missing checkpoint hash")
    if recorded_hash != _sha256(checkpoint):
        raise ValueError("existing checkpoint sidecar hash does not match checkpoint")
    return sidecar_path


def command_checkpoint_all(args: argparse.Namespace) -> int:
    """Bind, align, and optionally materialize every requested A checkpoint."""

    source = _read(args.source_events) if args.source_events else None
    target = _read(args.target_events) if args.target_events else None
    request_document = _read(args.requests)
    checkpoints = request_document.get("checkpoints") if isinstance(request_document, dict) else None
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ValueError("requests must contain a non-empty checkpoints list")
    if not args.plan_only and not args.command:
        raise ValueError("checkpoint-all requires a NEMU command unless --plan-only is used")
    identifiers = [item.get("id") for item in checkpoints if isinstance(item, dict)]
    if len(identifiers) != len(checkpoints) or any(not isinstance(item, str) or not item or Path(item).name != item or item in {".", ".."} for item in identifiers):
        raise ValueError("every checkpoint id must be a safe non-empty path component")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("checkpoint ids must be unique")

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "checkpoint-all-result.json"
    correspondence = _read(args.correspondence) if args.correspondence else {}
    run_manifest = getattr(args, "run_manifest", None)
    run_document = _read(run_manifest) if run_manifest else request_document.get("target_run")
    if run_document is None and target:
        run_document = target.get("run", target.get("manifest"))
    if not isinstance(run_document, dict):
        raise ValueError("target events do not contain a run manifest")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    items: list[dict[str, Any]] = []
    pending_materialization: list[tuple[dict[str, Any], dict[str, Any], Path, int]] = []
    target_position_counts: dict[tuple[str, int], int] = {}
    for checkpoint in checkpoints:
        pretarget = checkpoint.get("target_position")
        if isinstance(pretarget, dict):
            key = (str(pretarget.get("anchor_id")), int(pretarget.get("occurrence", -1)))
            target_position_counts[key] = target_position_counts.get(key, 0) + 1

    for checkpoint in checkpoints:
        identifier = checkpoint["id"]
        item_dir = output_root / identifier
        item_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {"id": identifier, "status": "failed", "reason": None}
        if checkpoint.get("source_checkpoint") is not None:
            record["source_checkpoint"] = checkpoint["source_checkpoint"]
        try:
            if checkpoint.get("status") == "rejected":
                record.update(status="rejected", reason=checkpoint.get("reason", "SOURCE_BINDING_REJECTED"))
                items.append(record)
                _write(summary_path, _batch_result(items, plan_only=args.plan_only))
                continue
            source_checkpoint = checkpoint.get("source_checkpoint")
            if source_checkpoint is not None and not Path(source_checkpoint).is_file():
                raise ValueError("source checkpoint does not exist")
            target_candidate = checkpoint.get("target_position")
            if isinstance(target_candidate, dict):
                key = (str(target_candidate.get("anchor_id")), int(target_candidate.get("occurrence", -1)))
                if target_position_counts.get(key, 0) > 1:
                    raise ValueError("TARGET_POSITION_COLLISION")
            if checkpoint.get("target_position"):
                source_position = checkpoint.get("source_position")
                target_position = checkpoint["target_position"]
                if not isinstance(source_position, dict) or not _bound_semantic_validation(checkpoint.get("semantic_validation"), source_position, target_position, run_document):
                    record.update(status="candidate", reason="TARGET_DYNAMIC_EVIDENCE_REQUIRED", semantic_validation="candidate")
                    items.append(record)
                    _write(summary_path, _batch_result(items, plan_only=args.plan_only))
                    continue
                if checkpoint.get("alignment_evidence", {}).get("method") not in {"dynamic_semantic_occurrence", "exact_boundary_source_marker_occurrence"}:
                    raise ValueError("prealigned target lacks dynamic semantic occurrence evidence")
                if target_position.get("build_id") != run_document.get("build_id"):
                    raise ValueError("prealigned positions do not match the target run")
                if "event_phase" in source_position or "event_phase" in target_position:
                    raise ValueError("event_phase belongs to the run manifest, not a position")
                bound = {"schema_version": 1, "status": checkpoint.get("position_status", "exact"), "source_position": source_position, "event": None, "reason": None}
                alignment_value = {"schema_version": 1, "status": "matched", "source": source_position, "correspondence": {"schema_version": 1, "source": source_position, "target": target_position, "position_status": bound["status"], "restore_status": "not_run", "cross_build_status": "not_run", "coverage_status": "not_run", "evidence": checkpoint["alignment_evidence"], "reason": None}, "global_path_margin": None, "top_paths": [], "manifest_hashes": checkpoint.get("manifest_hashes", {}), "diagnostics": {"method": "dynamic_semantic_occurrence"}}
                record["semantic_validation"] = "validated"
            else:
                request = checkpoint.get("request")
                if not isinstance(request, dict) or source is None or target is None:
                    raise ValueError("checkpoint request requires source and target event envelopes")
                bound = bind_source_position(source.get("events", []), request)
                if bound["status"] not in {"exact", "snapped"}:
                    record.update(status="rejected", reason=bound["reason"])
                    items.append(record)
                    _write(summary_path, _batch_result(items, plan_only=args.plan_only))
                    continue
                alignment_value = PositionAligner().align(source, target, correspondence, bound["source_position"], policy={"max_cells": args.max_cells, "max_seconds": args.max_seconds}).to_dict()
            source_position_path = item_dir / "source-position.json"
            _write(source_position_path, bound)
            record["source_position"] = str(source_position_path)
            alignment_path = item_dir / "alignment.json"
            _write(alignment_path, alignment_value)
            record["alignment"] = str(alignment_path)
            if alignment_value["status"] != "matched":
                record.update(status="rejected", reason=alignment_value["correspondence"]["reason"])
                items.append(record)
                _write(summary_path, _batch_result(items, plan_only=args.plan_only))
                continue

            target_position = alignment_value["correspondence"]["target"]
            semantic_validation = checkpoint.get("semantic_validation")
            if not _bound_semantic_validation(semantic_validation, bound["source_position"], target_position, run_document):
                record.update(status="candidate", reason="TARGET_DYNAMIC_EVIDENCE_REQUIRED", semantic_validation="candidate")
                items.append(record)
                _write(summary_path, _batch_result(items, plan_only=args.plan_only))
                continue
            target_path = write_target(item_dir / "target.json", target_position, run_manifest=run_document)
            record.update(status="aligned", target=str(target_path))
            record["semantic_validation"] = "validated"
            if not args.plan_only:
                materialization_dir = item_dir / "materialization"
                resumed = _resume_sidecar(materialization_dir, target_position, run_document, command) if args.resume else None
                if resumed:
                    record.update(status="reused", checkpoint_sidecar=str(resumed))
                else:
                    if list(materialization_dir.glob("**/*memory*")):
                        raise ValueError("materialization contains a checkpoint without a matching sidecar")
                    checkpoint_id = int(checkpoint.get("checkpoint_id", identifier.removeprefix("point-")))
                    pending_materialization.append((record, target_position, materialization_dir, checkpoint_id))
        except Exception as exc:
            if str(exc) == "TARGET_POSITION_COLLISION":
                record.update(status="rejected", reason="TARGET_POSITION_COLLISION")
            else:
                record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        items.append(record)
        _write(summary_path, _batch_result(items, plan_only=args.plan_only))

    if pending_materialization:
        batch_dir = output_root / "materialization"
        targets = [{"checkpoint_id": checkpoint_id, "target": target} for _, target, _, checkpoint_id in pending_materialization]
        materialized = run_nemu_targets(targets, command, run_manifest=run_document, output_dir=batch_dir)
        for record, target, materialization_dir, checkpoint_id in pending_materialization:
            materialization_dir.mkdir(parents=True, exist_ok=True)
            value = materialized["targets"].get(str(checkpoint_id))
            if materialized["exit_status"] != 0 or value is None:
                record.update(status="failed", reason=f"NEMU batch did not materialize checkpoint {checkpoint_id}")
                continue
            hit = value["hit"]
            sidecar = materialization_dir / "checkpoint-sidecar.json"
            _write(sidecar, {"schema_version": 1, "build_id": target["build_id"], "run_id": run_document["run_id"], "run_manifest_sha256": run_document.get("manifest_sha256"), "run_fingerprint": value["run_fingerprint"], "anchor_id": target["anchor_id"], "occurrence": hit["occurrence"], "pc": hit["pc"], "workload_icount": hit["workload_icount"], "interval": hit["workload_icount"] // int(run_document["interval_instructions"]), "offset": hit["workload_icount"] % int(run_document["interval_instructions"]), "checkpoint": value["checkpoint"], "checkpoint_sha256": value["checkpoint_sha256"], "marker_consumed": False})
            materialization_root = Path(materialized.get("attempt_dir", batch_dir))
            record.update(status="materialized", checkpoint_sidecar=str(sidecar), materialization=str(materialization_root / "materialization.json"))

    result = _batch_result(items, plan_only=args.plan_only)
    _write(summary_path, result)
    return 0 if result["status"] in {"planned", "complete"} else 2


def _checkpoint_point(path: Path) -> int:
    for parent in (path.parent, *path.parents):
        if parent.name.isdigit():
            return int(parent.name)
    raise ValueError(f"checkpoint path has no numeric point directory: {path}")


def _watch_candidates(catalog: Any) -> dict[str, Any]:
    by_pc: dict[int, list[Any]] = {}
    for anchor in catalog.anchors:
        if anchor.kind == "symbol" and anchor.name and anchor.ranges:
            by_pc.setdefault(min(item.start for item in anchor.ranges), []).append(anchor)
    return {anchors[0].semantic_key: anchors[0] for anchors in by_pc.values() if len(anchors) == 1}


def _write_watchlist(elf: Path, catalog_path: Path, output: Path, anchors: list[Any]) -> Path:
    watches = {str(watch_id): {"anchor_id": anchor.anchor_id, "semantic_key": anchor.semantic_key, "name": anchor.name, "pc": min(item.start for item in anchor.ranges)} for watch_id, anchor in enumerate(sorted(anchors, key=lambda item: item.semantic_key))}
    _write(output, {"schema_version": 1, "elf": str(elf.resolve()), "elf_sha256": _sha256(elf), "catalog": str(catalog_path.resolve()), "catalog_sha256": _sha256(catalog_path), "event_phase": "before_instruction", "watches": watches})
    return output


def command_prepare_watchlists(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_catalog = build_catalog(args.source_elf.resolve(), recover_loops=False)
    target_catalog = build_catalog(args.target_elf.resolve(), recover_loops=False)
    source_catalog_path, target_catalog_path = args.output_dir / "source-catalog.json", args.output_dir / "target-catalog.json"
    source_catalog.save(source_catalog_path); target_catalog.save(target_catalog_path)
    source_candidates, target_candidates = _watch_candidates(source_catalog), _watch_candidates(target_catalog)
    keys = set(source_candidates) & set(target_candidates)
    if args.anchor_name:
        names = set(args.anchor_name)
        keys = {key for key in keys if source_candidates[key].name in names}
        missing = names - {source_candidates[key].name for key in keys}
        if missing:
            raise ValueError(f"A/B ELFs lack unique common symbol anchors: {sorted(missing)}")
    if not keys:
        raise ValueError("A/B ELFs have no unique common symbol anchors")
    source = _write_watchlist(args.source_elf.resolve(), source_catalog_path, args.output_dir / "source-watchlist.json", [source_candidates[key] for key in keys])
    target = _write_watchlist(args.target_elf.resolve(), target_catalog_path, args.output_dir / "target-watchlist.json", [target_candidates[key] for key in keys])
    source_keys = {watch["semantic_key"] for watch in _read(source)["watches"].values()}
    target_keys = {watch["semantic_key"] for watch in _read(target)["watches"].values()}
    if source_keys != target_keys:
        raise ValueError("A/B selected anchors do not have identical semantic keys")
    _write(args.output_dir / "watchlists.json", {"schema_version": 1, "source": str(source), "target": str(target), "semantic_keys": sorted(source_keys)})
    return 0


def command_calibrate_source_checkpoints(args: argparse.Namespace) -> int:
    """Probe A boundaries and emit B target candidates pending dynamic validation."""

    source_watch_document = _read(args.source_watchlist_manifest)
    target_watch_document = _read(args.target_watchlist_manifest)
    source_watches = source_watch_document.get("watches", {})
    target_watches = target_watch_document.get("watches", {})
    if not source_watches or not target_watches:
        raise ValueError("source and target watchlist manifests must contain watches")
    target_by_key = {watch["semantic_key"]: watch for watch in target_watches.values()}
    if len(target_by_key) != len(target_watches):
        raise ValueError("target watchlist semantic keys must be unique")
    source_keys = {watch["semantic_key"] for watch in source_watches.values()}
    if source_keys != set(target_by_key):
        raise ValueError("A/B watchlists must contain the same semantic keys")
    source_run = _read(args.source_run_manifest) if args.source_run_manifest else {"workload_id": args.workload_id, "build_id": args.source_build_id, "run_id": f"{args.workload_id}-{args.source_build_id}-dynamic", "interval_instructions": args.interval_instructions, "event_phase": "before_instruction", "manifest_sha256": _sha256(args.source_watchlist_manifest), "elf_sha256": source_watch_document.get("elf_sha256")}
    target_run = _read(args.target_run_manifest) if args.target_run_manifest else {"workload_id": args.workload_id, "build_id": args.target_build_id, "run_id": f"{args.workload_id}-{args.target_build_id}-dynamic", "interval_instructions": args.interval_instructions, "event_phase": "before_instruction", "manifest_sha256": _sha256(args.target_watchlist_manifest), "elf_sha256": target_watch_document.get("elf_sha256")}
    if source_run.get("event_phase") != "before_instruction" or target_run.get("event_phase") != "before_instruction":
        raise ValueError("source and target runs must use before_instruction semantics")
    checkpoints = sorted(args.source_checkpoints.glob("**/*memory*"), key=lambda path: (_checkpoint_point(path), str(path)))
    points = [_checkpoint_point(path) for path in checkpoints]
    if not checkpoints or len(points) != len(set(points)):
        raise ValueError("source checkpoint directory must contain exactly one archive per point")

    output_root = args.output_dir.resolve(); output_root.mkdir(parents=True, exist_ok=True)
    last_boundary = max(0, max(points) * args.interval_instructions - args.warmup_instructions)
    max_instructions = args.max_instructions or args.boot_allowance + last_boundary + args.tail_instructions
    source_catalog_path = args.source_watchlist_manifest.with_name("source-catalog.json")
    target_catalog_path = args.target_watchlist_manifest.with_name("target-catalog.json")
    if not source_catalog_path.is_file() or not target_catalog_path.is_file():
        raise RuntimeError("exact source binding requires source-catalog.json and target-catalog.json")
    runner = NemuSourceProbeRunner(nemu=args.nemu, firmware=args.source_firmware, output_dir=output_root, interval_instructions=args.interval_instructions, max_instructions=max_instructions, context_size=args.context_size, timeout=args.timeout, force=args.force_probe, rng_seed=args.rng_seed)
    point_requests = tuple(CheckpointPoint(point, max(0, point * args.interval_instructions - args.warmup_instructions)) for point in points)
    bindings = SourceBoundaryResolver(runner).resolve(point_requests, source_run, AnchorCatalog.load(source_catalog_path), AnchorCatalog.load(target_catalog_path), BoundaryPolicy(args.interval_instructions, args.max_source_displacement, getattr(args, "multi_address_policy", "reject")))
    _write(output_root / "source-resolution.json", {"schema_version": 1, "status": "complete", "rng_seed": args.rng_seed, "items": [item.__dict__ for item in bindings.items], "counts": {"requested": len(bindings.items), "resolved": len(bindings.resolved), "rejected": len(bindings.items) - len(bindings.resolved)}, "boundary_artifact": bindings.boundary_artifact, "occurrence_artifact": bindings.occurrence_artifact})

    binding_by_id = {item.checkpoint_id: item for item in bindings.items}
    requests = []
    for checkpoint, point in zip(checkpoints, points):
        binding = binding_by_id[point]
        if binding.status != "resolved":
            requests.append({"id": f"point-{point}", "checkpoint_id": point, "source_checkpoint": str(checkpoint), "status": "rejected", "reason": binding.reason})
            continue
        marker_icount = int(binding.marker_hit_icount)
        delta = int(binding.marker_delta_instructions)
        source_position = {"build_id": source_run["build_id"], "anchor_id": binding.source_marker["anchor_id"], "occurrence": binding.occurrence, "pc": binding.source_marker["pc"], "workload_icount": marker_icount, "interval": marker_icount // args.interval_instructions, "offset": marker_icount % args.interval_instructions, "requested_icount": binding.requested_icount, "actual_delta_instructions": delta}
        target_position = {"build_id": target_run["build_id"], "anchor_id": binding.target_marker["anchor_id"], "occurrence": binding.occurrence, "pc": binding.target_marker["pc"], "workload_icount": None, "interval": None, "offset": None, "requested_icount": None, "actual_delta_instructions": None}
        evidence = {"method": "exact_boundary_source_marker_occurrence", "granularity": "source_line", "semantic_key": binding.semantic_key, "occurrence_scope": "global_from_scratch", "rng_seed": args.rng_seed, "boundary_pc": binding.boundary_pc, "marker_delta_instructions": delta, "boundary_observation_sha256": binding.boundary_observation_sha256, "occurrence_observation_sha256": binding.occurrence_observation_sha256, "boundary_artifact": bindings.boundary_artifact, "occurrence_artifact": bindings.occurrence_artifact, "multi_address_disambiguation": binding.multi_address_disambiguation}
        requests.append({"id": f"point-{point}", "checkpoint_id": point, "source_checkpoint": str(checkpoint), "source_position": source_position, "target_position": target_position, "position_status": "exact" if delta == 0 else "snapped", "alignment_evidence": evidence, "semantic_validation": {"status": "candidate", "reason": "TARGET_DYNAMIC_EVIDENCE_REQUIRED"}, "manifest_hashes": {"source": source_run.get("manifest_sha256"), "target": target_run.get("manifest_sha256")}})
    result = {"schema_version": 1, "status": "complete", "rng_seed": args.rng_seed, "checkpoints": requests, "counts": {"requested": len(checkpoints), "resolved": len(bindings.resolved), "rejected": len(bindings.items) - len(bindings.resolved), "failed": 0}, "source_run": source_run, "target_run": target_run, "probe_artifacts": {"boundary": bindings.boundary_artifact, "occurrence": bindings.occurrence_artifact}}
    _write(output_root / "source-calibration-result.json", result); _write(args.output, {"schema_version": 1, "target_run": target_run, "checkpoints": requests})
    return 0


def command_checkpoint_suite(args: argparse.Namespace) -> int:
    """Prepare a strict A-to-B checkpoint candidate plan for one suite workload."""

    workload_root = args.suite.resolve() / "workloads" / args.workload_id
    source_root = workload_root / args.source_label
    target_root = workload_root / args.target_label
    source_elf = source_root / "elf" / f"{args.workload_id}.elf"
    target_elf = target_root / "elf" / f"{args.workload_id}.elf"
    source_firmware = source_root / "bin" / f"{args.workload_id}.fw_payload.bin"
    target_firmware = target_root / "bin" / f"{args.workload_id}.fw_payload.bin"
    for path in (source_elf, target_elf, source_firmware, target_firmware, args.nemu, args.source_checkpoints):
        if not path.exists():
            raise FileNotFoundError(path)

    output = args.output_dir.resolve()
    watch_dir = output / "watchlists"
    command_prepare_watchlists(argparse.Namespace(source_elf=source_elf, target_elf=target_elf, output_dir=watch_dir, anchor_name=args.anchor_name))
    requests = output / "requests.json"
    max_instructions = args.max_instructions
    command_calibrate_source_checkpoints(argparse.Namespace(
        source_checkpoints=args.source_checkpoints,
        source_run_manifest=None,
        target_run_manifest=None,
        source_watchlist_manifest=watch_dir / "source-watchlist.json",
        target_watchlist_manifest=watch_dir / "target-watchlist.json",
        source_firmware=source_firmware,
        nemu=args.nemu,
        output_dir=output,
        output=requests,
        workload_id=args.workload_id,
        source_build_id=args.source_label,
        target_build_id=args.target_label,
        interval_instructions=args.interval_instructions,
        warmup_instructions=args.warmup_instructions,
        boot_allowance=args.boot_allowance,
        tail_instructions=args.tail_instructions,
        max_instructions=max_instructions,
        max_source_displacement=args.max_source_displacement,
        multi_address_policy=getattr(args, "multi_address_policy", "reject"),
        force_probe=args.force_probe,
        rng_seed=args.rng_seed,
        context_size=32,
        timeout=args.timeout,
    ))
    if max_instructions is None:
        points = [_checkpoint_point(path) for path in args.source_checkpoints.glob("**/*memory*")]
        max_instructions = args.boot_allowance + max(0, max(points) * args.interval_instructions - args.warmup_instructions) + args.tail_instructions
    target_max_instructions = getattr(args, "target_max_instructions", None)
    if not args.plan_only and target_max_instructions is None:
        raise ValueError("checkpoint-suite requires --target-max-instructions for B materialization")
    if target_max_instructions is None:
        target_max_instructions = max_instructions
    return command_checkpoint_all(argparse.Namespace(
        source_events=None,
        target_events=None,
        run_manifest=None,
        requests=requests,
        correspondence=None,
        output_dir=output / "checkpoints",
        plan_only=args.plan_only,
        resume=args.resume,
        max_cells=2_000_000,
        max_seconds=30.0,
        command=[] if args.plan_only else [str(args.nemu), str(target_firmware), "-b", "-I", str(target_max_instructions), "--rng-seed", args.rng_seed, "--checkpoint-format", "zstd"],
    ))


def command_validate(args: argparse.Namespace) -> int:
    alignment = _read(args.alignment)
    position_status = alignment.get("correspondence", {}).get("position_status")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    restore = validate_restore(_read(args.from_scratch_b), _read(args.restore_b))
    cross_build = validate_cross_build(_read(args.source_progress), _read(args.target_progress), position_status)
    coverage = validate_coverage(_read(args.full_coverage_b), _read(args.sample_coverage_b))
    _write(output_dir / "restore.json", restore)
    _write(output_dir / "cross-build.json", cross_build)
    _write(output_dir / "coverage.json", coverage)
    status = "validated" if all(item["status"] == "validated" for item in (restore, cross_build, coverage)) else "failed"
    _write(args.output, {"schema_version": 1, "status": status, "position_status": position_status, "restore_status": restore["status"], "cross_build_status": cross_build["status"], "coverage_status": coverage["status"]})
    return 0 if status == "validated" else 2


def command_report(args: argparse.Namespace) -> int:
    values = [_read(path) for path in args.inputs]
    _write(args.output, {"schema_version": 1, "artifacts": [{"path": str(path), "sha256": _sha256(path), "status": value.get("status")} for path, value in zip(args.inputs, values)], "counts": {"artifacts": len(values)}})
    return 0


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze"); freeze.add_argument("--elf", type=Path, required=True); freeze.add_argument("--manifest", type=Path, required=True); freeze.add_argument("--output", type=Path, required=True); freeze.add_argument("--workload-id"); freeze.add_argument("--build-id"); freeze.add_argument("--run-id"); freeze.add_argument("--input-id"); freeze.add_argument("--functional-path-id"); freeze.add_argument("--terminal-marker"); freeze.add_argument("--interval-instructions", type=int, default=20_000_000); freeze.set_defaults(func=command_freeze)
    collect = sub.add_parser("collect-events"); collect.add_argument("--run-manifest", type=Path, required=True); collect.add_argument("--trace", type=Path, required=True); collect.add_argument("--terminal-marker"); collect.add_argument("--output", type=Path, required=True); collect.set_defaults(func=command_collect_events)
    bind = sub.add_parser("bind-source"); bind.add_argument("--events", type=Path, required=True); bind.add_argument("--request", type=Path, required=True); bind.add_argument("--watchlist", type=Path); bind.add_argument("--requested-icount", type=int); bind.add_argument("--source-event-index", type=int, default=0); bind.add_argument("--output", type=Path, required=True); bind.set_defaults(func=command_bind_source)
    align = sub.add_parser("align-progress"); align.add_argument("--source-events", type=Path, required=True); align.add_argument("--target-events", type=Path, required=True); align.add_argument("--source-position", type=Path, required=True); align.add_argument("--correspondence", type=Path); align.add_argument("--output", type=Path, required=True); align.add_argument("--max-cells", type=int, default=2_000_000); align.add_argument("--max-seconds", type=float, default=30.0); align.set_defaults(func=command_align_progress)
    materialize = sub.add_parser("materialize-target"); materialize.add_argument("--alignment", type=Path, required=True); materialize.add_argument("--run-manifest", type=Path, required=True); materialize.add_argument("--target", type=Path, required=True); materialize.add_argument("--output-dir", type=Path, default=Path("materialization")); materialize.add_argument("command", nargs=argparse.REMAINDER); materialize.set_defaults(func=command_materialize)
    checkpoint_all = sub.add_parser("checkpoint-all", help="align and generate all requested target checkpoints"); checkpoint_all.add_argument("--source-events", type=Path); checkpoint_all.add_argument("--target-events", type=Path); checkpoint_all.add_argument("--run-manifest", type=Path); checkpoint_all.add_argument("--requests", type=Path, required=True); checkpoint_all.add_argument("--correspondence", type=Path); checkpoint_all.add_argument("--output-dir", type=Path, required=True); checkpoint_all.add_argument("--plan-only", action="store_true"); checkpoint_all.add_argument("--resume", action="store_true"); checkpoint_all.add_argument("--max-cells", type=int, default=2_000_000); checkpoint_all.add_argument("--max-seconds", type=float, default=30.0); checkpoint_all.add_argument("command", nargs=argparse.REMAINDER); checkpoint_all.set_defaults(func=command_checkpoint_all)
    watchlists = sub.add_parser("prepare-watchlists", help="build matching semantic anchor watchlists for two ELFs"); watchlists.add_argument("--source-elf", type=Path, required=True); watchlists.add_argument("--target-elf", type=Path, required=True); watchlists.add_argument("--output-dir", type=Path, required=True); watchlists.add_argument("--anchor-name", action="append", default=[]); watchlists.set_defaults(func=command_prepare_watchlists)
    calibrate = sub.add_parser("calibrate-source-checkpoints", help="probe A checkpoint boundaries from one dynamic run"); calibrate.add_argument("--source-checkpoints", type=Path, required=True); calibrate.add_argument("--source-run-manifest", type=Path); calibrate.add_argument("--target-run-manifest", type=Path); calibrate.add_argument("--source-watchlist-manifest", type=Path, required=True); calibrate.add_argument("--target-watchlist-manifest", type=Path, required=True); calibrate.add_argument("--source-firmware", type=Path, required=True); calibrate.add_argument("--nemu", type=Path, required=True); calibrate.add_argument("--output-dir", type=Path, required=True); calibrate.add_argument("--output", type=Path, required=True); calibrate.add_argument("--workload-id", default="mcf"); calibrate.add_argument("--source-build-id", default="A"); calibrate.add_argument("--target-build-id", default="B"); calibrate.add_argument("--interval-instructions", type=int, default=20_000_000); calibrate.add_argument("--warmup-instructions", type=int, default=20_000_000); calibrate.add_argument("--boot-allowance", type=int, default=200_000_000); calibrate.add_argument("--tail-instructions", type=int, default=20_000_000); calibrate.add_argument("--max-instructions", type=int); calibrate.add_argument("--max-source-displacement", dest="max_source_displacement", type=int, default=None); calibrate.add_argument("--multi-address-policy", choices=("reject", "identical-elf"), default="reject"); calibrate.add_argument("--force-probe", action="store_true"); calibrate.add_argument("--rng-seed", default=hashlib.sha256(b"checkpoint-align").hexdigest()); calibrate.add_argument("--context-size", type=int, default=32); calibrate.add_argument("--timeout", type=int, default=86_400); calibrate.set_defaults(func=command_calibrate_source_checkpoints)
    suite = sub.add_parser("checkpoint-suite", help="calibrate and materialize all A checkpoints with dynamic semantic occurrences"); suite.add_argument("--suite", type=Path, required=True); suite.add_argument("--workload-id", default="mcf"); suite.add_argument("--source-label", default="A"); suite.add_argument("--target-label", default="B"); suite.add_argument("--source-checkpoints", type=Path, required=True); suite.add_argument("--nemu", type=Path, required=True); suite.add_argument("--output-dir", type=Path, required=True); suite.add_argument("--anchor-name", action="append", default=[]); suite.add_argument("--interval-instructions", type=int, default=20_000_000); suite.add_argument("--warmup-instructions", type=int, default=20_000_000); suite.add_argument("--boot-allowance", type=int, default=200_000_000); suite.add_argument("--tail-instructions", type=int, default=20_000_000); suite.add_argument("--max-instructions", type=int); suite.add_argument("--target-max-instructions", type=int); suite.add_argument("--max-source-displacement", dest="max_source_displacement", type=int, default=None); suite.add_argument("--multi-address-policy", choices=("reject", "identical-elf"), default="reject"); suite.add_argument("--force-probe", action="store_true"); suite.add_argument("--rng-seed", default=hashlib.sha256(b"checkpoint-align").hexdigest()); suite.add_argument("--timeout", type=int, default=86_400); suite.add_argument("--plan-only", action="store_true"); suite.add_argument("--resume", action="store_true"); suite.set_defaults(func=command_checkpoint_suite)
    validate = sub.add_parser("validate"); validate.add_argument("--alignment", type=Path, required=True); validate.add_argument("--from-scratch-b", type=Path, required=True); validate.add_argument("--restore-b", type=Path, required=True); validate.add_argument("--source-progress", type=Path, required=True); validate.add_argument("--target-progress", type=Path, required=True); validate.add_argument("--full-coverage-b", type=Path, required=True); validate.add_argument("--sample-coverage-b", type=Path, required=True); validate.add_argument("--output-dir", type=Path, required=True); validate.add_argument("--output", type=Path, required=True); validate.set_defaults(func=command_validate)
    report = sub.add_parser("report"); report.add_argument("--inputs", type=Path, nargs="+", required=True); report.add_argument("--output", type=Path, required=True); report.set_defaults(func=command_report)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
