#!/usr/bin/env python3
"""Thin dynamic-progress orchestration entry point.

The subcommands deliberately keep collection, binding, alignment,
materialization, and validation as separate artifacts.  The legacy
``cross_elf_checkpoint.py`` entry point remains interval/BBV compatibility
only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiment.dwarf_source import OccurrenceTrace
from experiment.position_aligner import (
    BuildRun,
    PositionAligner,
    ProgressEvent,
)
from experiment.position_aligner.materialize import run_nemu_target, write_target
from experiment.position_aligner.validation import validate_coverage, validate_cross_build, validate_restore


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
    raw = _read(args.run_manifest)
    terminal_marker = args.terminal_marker or raw.get("terminal_marker")
    terminal_complete = raw.get("terminal_observed") is True
    if terminal_marker and not terminal_complete:
        run_dir = args.run_manifest.parent
        terminal_complete = all(
            terminal_marker in (run_dir / f"stdout-{record['run_index']}.log").read_text(encoding="utf-8", errors="replace")
            or terminal_marker in (run_dir / f"stderr-{record['run_index']}.log").read_text(encoding="utf-8", errors="replace")
            for record in raw.get("runs", ())
        )
    plugin_complete = terminal_complete and all(record.get("exit_status") == 0 and record.get("plugin_status", {}).get("vcpus") == 1 and not record.get("plugin_status", {}).get("budget_exceeded") and not record.get("plugin_status", {}).get("write_error") and not record.get("plugin_status", {}).get("close_error") for record in raw.get("runs", ()))
    events = []
    for event in trace.events:
        events.append(ProgressEvent(run.build_id, run.run_id, event.anchor_id, event.semantic_key or event.anchor_id, event.occurrence, event.event_phase, event.pc or 0, event.workload_icount, {"source": event.source if hasattr(event, "source") else None}).to_dict())
    _write(args.output, {"schema_version": 1, "evidence_kind": "dynamic_execution", "complete": plugin_complete, "run": run.to_dict(), "events": events, "source_trace_sha256": _sha256(args.trace)})
    return 0


def bind_source_position(events: list[dict[str, Any]], requested: dict[str, Any]) -> dict[str, Any]:
    phase = requested.get("event_phase", "before_instruction")
    matches: list[dict[str, Any]] = []
    window = requested.get("window_events")
    if window:
        def identity(event: dict[str, Any]) -> tuple[Any, ...]:
            return event.get("semantic_key"), event.get("event_phase"), event.get("pc")
        signature = [identity(event) for event in window]
        starts = [index for index in range(len(events) - len(signature) + 1) if [identity(event) for event in events[index:index + len(signature)]] == signature]
        offset = int(requested.get("source_event_offset", len(signature) // 2))
        if offset < 0 or offset >= len(signature):
            raise ValueError("source_event_offset is outside window_events")
        matches = [events[start + offset] for start in starts]
    elif requested.get("occurrence_scope") == "global_from_scratch":
        matches = [event for event in events if event.get("event_phase") == phase and event.get("anchor_id") == requested.get("anchor_id") and event.get("occurrence") == requested.get("occurrence")]
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
    source_position = {"build_id": event["build_id"], "anchor_id": event["anchor_id"], "occurrence": event["occurrence"], "event_phase": event["event_phase"], "pc": observed_pc, "workload_icount": icount, "interval": (icount // interval_size) if icount is not None else None, "offset": (icount % interval_size) if icount is not None else None, "requested_icount": requested_icount if icount is not None else None, "actual_delta_instructions": (icount - requested_icount) if icount is not None else None}
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
            "event_phase": requested["event_phase"],
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
    target = result.get("correspondence", {}).get("target")
    if not target:
        raise ValueError("alignment has no B target to materialize")
    run_document = _read(args.run_manifest)
    run = run_document.get("run", run_document)
    target_path = write_target(args.target, target, run_manifest=run)
    if not args.command:
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    materialized = run_nemu_target(target_path, command, run_manifest=run, output_dir=args.output_dir)
    return 0 if materialized["exit_status"] == 0 else materialized["exit_status"]


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
    validate = sub.add_parser("validate"); validate.add_argument("--alignment", type=Path, required=True); validate.add_argument("--from-scratch-b", type=Path, required=True); validate.add_argument("--restore-b", type=Path, required=True); validate.add_argument("--source-progress", type=Path, required=True); validate.add_argument("--target-progress", type=Path, required=True); validate.add_argument("--full-coverage-b", type=Path, required=True); validate.add_argument("--sample-coverage-b", type=Path, required=True); validate.add_argument("--output-dir", type=Path, required=True); validate.add_argument("--output", type=Path, required=True); validate.set_defaults(func=command_validate)
    report = sub.add_parser("report"); report.add_argument("--inputs", type=Path, nargs="+", required=True); report.add_argument("--output", type=Path, required=True); report.set_defaults(func=command_report)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
