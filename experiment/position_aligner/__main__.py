"""Replay the M1 BBV locator against a prepared multi-workload suite.

Example::

    python3 -m experiment.position_aligner replay \
      --suite experiment/multi-workload --output /tmp/m1-replay.json

The command only reads profile/manifest/simpoint files and writes a derived
JSON report.  It never creates checkpoints or touches ``weights0``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .bbv import AlignmentPolicy, align, build_index


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _simpoint_indices(path: Path) -> list[int]:
    points = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields:
            points.append(int(fields[0]))
    return sorted(set(points))


def _run_spec(
    suite: Path,
    suite_manifest: dict[str, Any],
    suite_manifest_hash: str,
    workload: str,
    side: str,
    interval: int,
) -> dict[str, Any]:
    """Build a provenance-bound BBV run spec from the prepared suite."""

    base = suite / "workloads" / workload / side
    copied = suite_manifest.get("copied", {}).get(workload, {}).get(side, {})

    def artifact_path(name: str, relative: Path) -> Path:
        recorded = copied.get(name, {}).get("path")
        if not recorded:
            return base / relative
        path = Path(recorded)
        if not path.is_absolute():
            return suite / path
        # Older manifests used absolute paths.  Prefer them when still valid,
        # then fall back to the suite-local copy after relocation.
        return path if path.is_file() else base / relative

    metadata_path = artifact_path("json", Path("json") / f"{workload}.json")
    metadata = _read_json(metadata_path)
    total = metadata.get(workload, {}).get("insts")
    elf_entry = copied.get("elf", {})
    script_entry = copied.get("run_script", {})
    elf_sha = elf_entry.get("sha256")
    script_sha = script_entry.get("sha256")
    # The side label remains the selector used by align(); the artifact hash
    # is the immutable build identity used for audit and cross-run binding.
    spec: dict[str, Any] = {
        "build_id": side,
        "build_identity": f"elf-sha256:{elf_sha}" if elf_sha else None,
        "artifact_sha256": elf_sha,
        "run_id": f"{workload}-{side}-profile",
        "bbv_path": artifact_path("bbv", Path("profiling") / "simpoint_bbv.gz"),
        "interval_instructions": interval,
        "total_instructions": int(total) if total is not None else None,
        "workload_id": workload,
        "icount_domain": "workload_relative_instructions",
        "manifest_hash": suite_manifest_hash,
        # The prepared run script is the common functional-path/input
        # fingerprint available in this offline suite.
        "input_fingerprint": f"{workload}:{script_sha}" if script_sha else workload,
        "functional_path": script_sha,
        "manifest": {
            "workload_id": workload,
            "icount_domain": "workload_relative_instructions",
            "manifest_hash": suite_manifest_hash,
            "elf_sha256": elf_sha,
            "run_script_sha256": script_sha,
        },
    }
    return {key: value for key, value in spec.items() if value is not None}


def _side_labels(manifest: dict[str, Any]) -> tuple[str, str]:
    sides = manifest.get("sides", {})
    return str(sides.get("source", "A")), str(sides.get("target", "B"))


def replay_suite(suite: Path, workloads: list[str] | None = None,
                 policy: AlignmentPolicy | None = None) -> dict[str, Any]:
    manifest = _read_json(suite / "suite-manifest.json")
    suite_manifest_hash = _sha256(suite / "suite-manifest.json")
    interval = int(manifest.get("interval_instructions", 20_000_000))
    selected = workloads or list(manifest.get("workloads", []))
    source_side, target_side = _side_labels(manifest)
    reports: list[dict[str, Any]] = []
    for workload in selected:
        runs = [
            _run_spec(suite, manifest, suite_manifest_hash, workload, side, interval)
            for side in (source_side, target_side)
        ]
        index = build_index(
            runs,
            workload={
                "workload_id": workload,
                "icount_domain": "workload_relative_instructions",
                "manifest_hash": suite_manifest_hash,
            },
            policy=policy,
        )
        points = _simpoint_indices(
            suite / "workloads" / workload / source_side / "cluster" / "simpoints0"
        )
        result = align(index, source_side, [target_side], points)
        target = result["results"][target_side]
        status_counts = Counter(position["status"] for position in target.get("positions", []))
        reason_counts = Counter(
            position.get("reason") for position in target.get("positions", [])
            if position.get("status") == "rejected" and position.get("reason")
        )
        reports.append({
            "workload": workload,
            "source_side": source_side,
            "target_side": target_side,
            "source_points": len(points),
            "status": target.get("status"),
            "reason": target.get("reason"),
            "position_status_counts": dict(status_counts),
            "position_reject_reasons": dict(reason_counts),
            "global_path_score": target.get("global_path_score"),
            "global_path_margin": target.get("global_path_margin"),
            "result": target,
        })
    return {
        "schema_version": 1,
        "method": "sparse-global-bbv-dp",
        "policy": (policy or AlignmentPolicy()).as_dict(),
        "suite_manifest_sha256": suite_manifest_hash,
        "workloads": reports,
        "totals": {
            "workloads": len(reports),
            "source_points": sum(item["source_points"] for item in reports),
            "matched_positions": sum(item["position_status_counts"].get("matched", 0) for item in reports),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="replay all requested BBV points in a prepared suite")
    replay.add_argument("--suite", type=Path, required=True)
    replay.add_argument("--workloads", nargs="+")
    replay.add_argument("--output", type=Path)
    replay.add_argument("--max-seconds", type=float, default=120.0)
    replay.add_argument("--max-memory-bytes", type=int, default=512 * 1024 * 1024)
    replay.add_argument("--max-window", type=int, default=128)
    replay.add_argument("--min-score", type=float, default=0.55)
    replay.add_argument("--min-global-margin", type=float, default=0.10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "replay":
        policy = AlignmentPolicy(
            max_seconds=args.max_seconds,
            max_memory_bytes=args.max_memory_bytes,
            max_window=args.max_window,
            min_score=args.min_score,
            min_global_margin=args.min_global_margin,
        )
        report = replay_suite(args.suite.resolve(), args.workloads, policy)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")


if __name__ == "__main__":
    main()
