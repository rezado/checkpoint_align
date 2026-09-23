#!/usr/bin/env python3
"""Collect sparse function-entry occurrences with a qemu-user TCG plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from checkpoint_align.dwarf_source import (
    AnchorCatalog,
    canonical_json_hash,
    collect_occurrence_trace,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "occurrence_plugin.c"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tool_version(path: Path) -> str:
    completed = subprocess.run([str(path), "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    return completed.stdout.splitlines()[0]


def build_plugin(args: argparse.Namespace) -> int:
    glib_cflags = subprocess.run(
        ["pkg-config", "--cflags", "glib-2.0"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ).stdout.split()
    command = [
        args.cc,
        "-std=c11",
        "-D_GNU_SOURCE",
        "-O2",
        "-fPIC",
        "-shared",
        "-Wall",
        "-Wextra",
        "-Werror",
        f"-I{args.include}",
        *glib_cflags,
        str(args.source),
        "-o",
        str(args.output),
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)
    print(json.dumps({"output": str(args.output), "sha256": sha256_file(args.output)}))
    return 0


def select_entries(catalog: AnchorCatalog, anchor_ids: set[str], anchor_names: set[str]) -> list[tuple[int, Any]]:
    selected: list[tuple[int, Any]] = []
    matched_names: set[str] = set()
    matched_ids: set[str] = set()
    for anchor in catalog.anchors:
        if not anchor.ranges or anchor.kind not in {"function", "symbol", "loop", "loop_candidate"}:
            continue
        if anchor.anchor_id not in anchor_ids and anchor.name not in anchor_names:
            continue
        selected.append((min(item.start for item in anchor.ranges), anchor))
        matched_ids.add(anchor.anchor_id)
        matched_names.add(anchor.name)
    missing_ids = anchor_ids - matched_ids
    missing_names = anchor_names - matched_names
    if missing_ids or missing_names:
        raise ValueError(f"anchors not found: ids={sorted(missing_ids)}, names={sorted(missing_names)}")
    if not selected:
        raise ValueError("at least one --anchor-id or --anchor-name is required")
    all_at_pc: dict[int, list[Any]] = {}
    for anchor in catalog.anchors:
        if anchor.kind in {"function", "symbol", "loop", "loop_candidate"} and anchor.ranges:
            all_at_pc.setdefault(min(item.start for item in anchor.ranges), []).append(anchor)
    by_pc: dict[int, Any] = {}
    for pc, anchor in selected:
        if len({item.anchor_id for item in all_at_pc.get(pc, [])}) > 1:
            raise ValueError(f"PC 0x{pc:x} maps to multiple catalog anchors; watchlist is ambiguous")
        old = by_pc.get(pc)
        if old is not None and old.anchor_id != anchor.anchor_id:
            raise ValueError(f"PC 0x{pc:x} maps to multiple selected anchors")
        by_pc[pc] = anchor
    return sorted(by_pc.items())


def write_watchlist(
    output_dir: Path,
    elf: Path,
    catalog_path: Path,
    input_manifest: Path,
    catalog: AnchorCatalog,
    entries: list[tuple[int, Any]],
) -> tuple[Path, Path, dict[int, dict[str, Any]]]:
    watchlist_path = output_dir / "watchlist.tsv"
    manifest_path = output_dir / "watchlist-manifest.json"
    watches: dict[int, dict[str, Any]] = {}
    rows = []
    for watch_id, (pc, anchor) in enumerate(entries):
        rows.append(f"{watch_id}\t0x{pc:x}\n")
        source = anchor.source.to_dict() if anchor.source else None
        watches[watch_id] = {
            "anchor_id": anchor.anchor_id,
            "semantic_key": anchor.semantic_key,
            "confidence": anchor.confidence,
            "kind": anchor.kind,
            "name": anchor.name,
            "pc": pc,
            "source": source,
        }
    watchlist_path.write_text("".join(rows), encoding="ascii")
    manifest = {
        "schema_version": 1,
        "event_phase": "before_instruction",
        "occurrence_base": 0,
        "elf_sha256": sha256_file(elf),
        "catalog_sha256": sha256_file(catalog_path),
        "catalog_canonical_sha256": canonical_json_hash(catalog.to_dict()),
        "input_manifest_sha256": sha256_file(input_manifest),
        "watchlist_sha256": sha256_file(watchlist_path),
        "watches": {str(key): value for key, value in watches.items()},
    }
    write_json(manifest_path, manifest)
    return watchlist_path, manifest_path, watches


def load_anchor_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def decode_trace(path: Path, watches: dict[int, dict[str, Any]], max_events: int) -> list[dict[str, Any]]:
    size = path.stat().st_size
    if size % 4:
        raise RuntimeError(f"truncated stream: {path} has {size} bytes")
    count = size // 4
    if count > max_events:
        raise RuntimeError(f"event budget exceeded: {count} > {max_events}")
    samples = []
    with path.open("rb") as handle:
        for (watch_id,) in struct.iter_unpack("<I", handle.read()):
            watch = watches.get(watch_id)
            if watch is None:
                raise RuntimeError(f"illegal watch ID {watch_id}")
            samples.append(
                {
                    "pc": watch["pc"],
                    "anchor_id": watch["anchor_id"],
                }
            )
    return samples


def normalized_events(trace: Any) -> list[dict[str, Any]]:
    return [event.to_dict() for event in trace.events]


def collect(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    elf = args.elf.resolve()
    catalog_path = args.catalog.resolve()
    input_manifest = args.manifest.resolve()
    input_metadata = json.loads(input_manifest.read_text(encoding="utf-8"))
    terminal_marker = args.terminal_marker or input_metadata.get("terminal_marker", "HIT GOOD TRAP")
    qemu = args.qemu.resolve()
    plugin = args.plugin.resolve()
    catalog = AnchorCatalog.load(catalog_path)
    if catalog.artifact_sha256 != sha256_file(elf):
        raise RuntimeError("catalog ELF hash does not match --elf")
    anchor_ids = set(args.anchor_id)
    for path in args.anchor_id_file:
        anchor_ids.update(load_anchor_ids(path))
    entries = select_entries(catalog, anchor_ids, set(args.anchor_name))
    watchlist_path, watch_manifest_path, watches = write_watchlist(
        output_dir, elf, catalog_path, input_manifest, catalog, entries
    )
    run_records = []
    traces = []
    guest_args = args.guest_args[1:] if args.guest_args[:1] == ["--"] else args.guest_args
    for run_index in range(1, args.repeat + 1):
        trace_path = output_dir / f"trace-{run_index}.u32"
        status_path = output_dir / f"plugin-status-{run_index}.json"
        stdout_path = output_dir / f"stdout-{run_index}.log"
        stderr_path = output_dir / f"stderr-{run_index}.log"
        occurrence_path = output_dir / f"occurrences-{run_index}.json"
        trace_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
        command = [
            str(qemu),
            "-plugin",
            f"{plugin},watchlist={watchlist_path},trace={trace_path},status={status_path},max-events={args.max_events},max-per-watch={args.max_per_watch}",
            str(elf),
            *guest_args,
        ]
        start = time.monotonic()
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=args.timeout, check=False)
        elapsed = time.monotonic() - start
        if completed.returncode != 0:
            raise RuntimeError(f"qemu run {run_index} failed with exit status {completed.returncode}")
        if not status_path.is_file():
            raise RuntimeError(f"qemu run {run_index} did not produce plugin completion status")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if (
            status.get("vcpus") != 1
            or status.get("budget_exceeded")
            or status.get("write_error")
            or status.get("close_error")
            or status.get("truncated_watch_ids")
        ):
            raise RuntimeError(f"invalid plugin status for run {run_index}: {status}")
        samples = decode_trace(trace_path, watches, args.max_events)
        if status.get("events") != len(samples):
            raise RuntimeError(f"plugin event count disagrees with stream for run {run_index}")
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        terminal_observed = terminal_marker in stdout_text or terminal_marker in stderr_text
        if not terminal_observed:
            raise RuntimeError(f"qemu run {run_index} did not reach terminal marker {terminal_marker!r}")
        occurrence = collect_occurrence_trace(
            catalog,
            samples,
            mode="function-entry",
            source=str(trace_path),
        )
        if occurrence.unmatched_samples:
            raise RuntimeError(f"decoder produced {occurrence.unmatched_samples} unmatched samples")
        occurrence.metadata.update(
            {
                "collector": "qemu-user-occurrence-plugin-v1",
                "raw_stream_sha256": sha256_file(trace_path),
                "raw_stream_bytes": trace_path.stat().st_size,
                "watchlist_manifest_sha256": sha256_file(watch_manifest_path),
                "terminal_marker": terminal_marker,
                "terminal_observed": terminal_observed,
            }
        )
        occurrence.save(occurrence_path)
        traces.append(occurrence)
        run_records.append(
            {
                "run_index": run_index,
                "command": command,
                "elapsed_seconds": elapsed,
                "exit_status": completed.returncode,
                "event_count": len(samples),
                "trace_bytes": trace_path.stat().st_size,
                "trace_sha256": sha256_file(trace_path),
                "occurrences_sha256": sha256_file(occurrence_path),
                "stdout_sha256": sha256_file(stdout_path),
                "stderr_sha256": sha256_file(stderr_path),
                "terminal_observed": terminal_observed,
                "plugin_status": status,
            }
        )
    stable = all(normalized_events(trace) == normalized_events(traces[0]) for trace in traces[1:])
    output_stable = all(item["stdout_sha256"] == run_records[0]["stdout_sha256"] for item in run_records[1:])
    if not stable or not output_stable:
        raise RuntimeError("repeated runs produced different occurrence traces or stdout")
    traces[0].save(output_dir / "occurrences.json")
    run_manifest = {
        "schema_version": 1,
        "collector": "qemu-user-occurrence-plugin-v1",
        "workload_id": args.workload_id or input_metadata.get("workload_id", "unknown"),
        "build_id": args.build_id or input_metadata.get("build_id", sha256_file(elf)),
        "run_id": args.run_id or input_metadata.get("run_id", f"{args.workload_id or input_metadata.get('workload_id', 'unknown')}-{args.build_id or input_metadata.get('build_id', 'build')}-run1"),
        "input_id": args.input_id or input_metadata.get("input_id", sha256_file(input_manifest)),
        "functional_path_id": args.functional_path_id or input_metadata.get("functional_path_id", "default"),
        "event_phase": "before_instruction",
        "occurrence_base": 0,
        "icount_domain": "workload_relative_instructions",
        "interval_instructions": args.interval_instructions,
        "interval_index_base": 0,
        "args": guest_args,
        "environment": {key: os.environ[key] for key in args.record_env if key in os.environ},
        "artifacts": input_metadata.get("artifacts", {}),
        "tools": {"qemu": {"sha256": sha256_file(qemu), "version": tool_version(qemu)}, "plugin": {"sha256": sha256_file(plugin), "version": "qemu-user-occurrence-plugin-v1"}},
        "input_manifest_sha256": sha256_file(input_manifest),
        "elf_sha256": sha256_file(elf),
        "catalog_sha256": sha256_file(catalog_path),
        "watchlist_manifest_sha256": sha256_file(watch_manifest_path),
        "qemu_sha256": sha256_file(qemu),
        "plugin_sha256": sha256_file(plugin),
        "terminal_marker": terminal_marker,
        "terminal_observed": all(item["terminal_observed"] for item in run_records),
        "event_trace_sha256": sha256_file(output_dir / "occurrences.json"),
        "manifest_sha256": sha256_file(input_manifest),
        "repeat_count": args.repeat,
        "normalized_trace_stable": stable,
        "stdout_stable": output_stable,
        "runs": run_records,
    }
    write_json(output_dir / "run-manifest.json", run_manifest)
    print(json.dumps({"output": str(output_dir), "events": len(traces[0].events), "stable": stable}))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-plugin")
    build.add_argument("--cc", default="gcc")
    build.add_argument("--include", type=Path, required=True)
    build.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    build.add_argument("--output", type=Path, required=True)
    build.set_defaults(func=build_plugin)
    run = commands.add_parser("collect")
    run.add_argument("--qemu", type=Path, required=True)
    run.add_argument("--plugin", type=Path, required=True)
    run.add_argument("--elf", type=Path, required=True)
    run.add_argument("--catalog", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--anchor-id", action="append", default=[])
    run.add_argument("--anchor-id-file", type=Path, action="append", default=[])
    run.add_argument("--anchor-name", action="append", default=[])
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--max-events", type=int, default=10_000_000)
    run.add_argument("--max-per-watch", type=int, default=2**63 - 1)
    run.add_argument("--record-env", action="append", default=[])
    run.add_argument("--terminal-marker")
    run.add_argument("--workload-id")
    run.add_argument("--build-id")
    run.add_argument("--run-id")
    run.add_argument("--input-id")
    run.add_argument("--functional-path-id")
    run.add_argument("--interval-instructions", type=int, default=20_000_000)
    run.add_argument("guest_args", nargs=argparse.REMAINDER)
    run.set_defaults(func=collect)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
