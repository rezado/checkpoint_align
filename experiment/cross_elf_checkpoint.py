#!/usr/bin/env python3
"""Prepare, align, and validate checkpoints for arbitrary workload suites.

The suite manifest is the interface between preparation and later commands.
The default layout matches the existing profile exports, while workload names
and source/target build labels are discovered or configured at preparation
time instead of being embedded in the workflow.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path


DEFAULT_INTERVAL = 20_000_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file(source: Path, target: Path) -> dict:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {"path": str(target), "size": target.stat().st_size, "sha256": sha256(target)}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _profile_path(root: Path, *relative: str) -> Path:
    candidates = [root / item for item in relative]
    return next((path for path in candidates if path.is_file()), candidates[0])


def workload_files(root: Path, workload: str) -> dict[str, Path]:
    return {
        "elf": root / "elf" / f"{workload}.elf",
        "firmware": root / "bin" / f"{workload}.fw_payload.bin",
        "run_script": root / "cmd" / f"{workload}.run.sh",
        "json": root / "json" / f"{workload}.json",
        "simpoints": _profile_path(root, f"cluster/{workload}/simpoints0", "cluster/simpoints0"),
        "weights": _profile_path(root, f"cluster/{workload}/weights0", "cluster/weights0"),
        "bbv": _profile_path(root, f"profiling/{workload}/simpoint_bbv.gz", "profiling/simpoint_bbv.gz"),
        "profiling_log": _profile_path(
            root, f"logs/profiling/{workload}/profiling.out.log", "logs/profiling.out.log"
        ),
        "build_log": _profile_path(root, f"logs/build_elf/{workload}.log", "logs/build.log"),
    }


def local_files(root: Path, workload: str, side: str) -> dict[str, Path]:
    base = root / "workloads" / workload / side
    return {
        "elf": base / "elf" / f"{workload}.elf",
        "firmware": base / "bin" / f"{workload}.fw_payload.bin",
        "run_script": base / "cmd" / f"{workload}.run.sh",
        "json": base / "json" / f"{workload}.json",
        "simpoints": base / "cluster" / "simpoints0",
        "weights": base / "cluster" / "weights0",
        "bbv": base / "profiling" / "simpoint_bbv.gz",
        "profiling_log": base / "logs" / "profiling.out.log",
        "build_log": base / "logs" / "build.log",
    }


def discover_workloads(source_root: Path, target_root: Path) -> list[str]:
    """Return workload names with the required profile inputs on both sides."""

    source = {path.stem for path in (source_root / "json").glob("*.json")}
    target = {path.stem for path in (target_root / "json").glob("*.json")}
    workloads = []
    for workload in sorted(source & target):
        required = ("elf", "firmware", "json", "simpoints", "bbv")
        source_files = workload_files(source_root, workload)
        target_files = workload_files(target_root, workload)
        if all(source_files[name].is_file() and target_files[name].is_file() for name in required):
            workloads.append(workload)
    return workloads


def side_labels(manifest: dict) -> tuple[str, str]:
    sides = manifest.get("sides", {})
    return str(sides.get("source", "A")), str(sides.get("target", "B"))


def command_prepare(args: argparse.Namespace) -> None:
    suite = args.suite.resolve()
    source_root = args.source_root.resolve()
    target_root = args.target_root.resolve()
    workloads = args.workloads or discover_workloads(source_root, target_root)
    if not workloads:
        raise ValueError("no workload has the required profile inputs on both sides")
    source_side, target_side = args.source_side, args.target_side
    if source_side == target_side:
        raise ValueError("source and target side labels must differ")
    copied = {}
    required_artifacts = {"elf", "firmware", "json", "simpoints", "bbv"}
    for workload in workloads:
        copied[workload] = {}
        for side, profile_root in ((source_side, source_root), (target_side, target_root)):
            copied[workload][side] = {}
            sources = workload_files(profile_root, workload)
            targets = local_files(suite, workload, side)
            for name in sources:
                if not sources[name].is_file():
                    if name in required_artifacts:
                        raise FileNotFoundError(sources[name])
                    continue
                copied[workload][side][name] = copy_file(sources[name], targets[name])

    tools = {}
    if args.nemu:
        tools["nemu"] = copy_file(args.nemu.resolve(), suite / "tools" / "riscv64-nemu-interpreter")
        (suite / "tools" / "riscv64-nemu-interpreter").chmod(0o755)
    if args.gcpt:
        tools["gcpt"] = copy_file(args.gcpt.resolve(), suite / "tools" / "gcpt.bin")

    manifest = {
        "schema_version": 1,
        "source_profile_root": str(source_root),
        "target_profile_root": str(target_root),
        "interval_instructions": args.interval,
        "workloads": workloads,
        "sides": {"source": source_side, "target": target_side},
        "copied": copied,
        "tools": tools,
    }
    write_json(suite / "suite-manifest.json", manifest)
    print(json.dumps({"suite": str(suite), "workloads": workloads, "sides": manifest["sides"]}, indent=2))


def parse_points(path: Path) -> list[tuple[int, int]]:
    return [tuple(map(int, line.split()[:2])) for line in path.read_text().splitlines() if line.strip()]


def instruction_total(path: Path, workload: str) -> int:
    return int(read_json(path)[workload]["insts"])


def collect_rows(path: Path, wanted: set[int]) -> tuple[dict[int, list[int]], int]:
    rows = {}
    row_count = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            row_count = index + 1
            if index not in wanted:
                continue
            rows[index] = [
                int(fields[-1])
                for token in line.split()
                if len(fields := token.split(":")) >= 3
            ]
    return rows, row_count


def overlap(a: list[int], b: list[int]) -> float:
    denominator = max(len(a), len(b))
    return sum((Counter(a) & Counter(b)).values()) / denominator if denominator else 1.0


def align_workload(
    suite: Path,
    workload: str,
    source_side: str,
    target_side: str,
    interval: int,
    radius: int,
    context: int,
    min_score: float,
    min_margin: float,
    requested_points: list[int] | None,
) -> dict:
    a = local_files(suite, workload, source_side)
    b = local_files(suite, workload, target_side)
    all_points = parse_points(a["simpoints"])
    if requested_points is not None:
        selected = set(requested_points)
        points = [(point, cluster) for point, cluster in all_points if point in selected]
        missing = selected - {point for point, _ in points}
        if missing:
            raise ValueError(f"{workload}: requested points are not in {source_side} SimPoints: {sorted(missing)}")
    else:
        points = all_points

    a_total = instruction_total(a["json"], workload)
    b_total = instruction_total(b["json"], workload)
    ratio = b_total / a_total
    plans = []
    a_wanted = set()
    b_wanted = set()
    for point, cluster in points:
        center = round(point * ratio)
        candidates = range(max(0, center - radius), center + radius + 1)
        plans.append((point, cluster, center, list(candidates)))
        for delta in range(-context, context + 1):
            if point + delta >= 0:
                a_wanted.add(point + delta)
            for candidate in candidates:
                if candidate + delta >= 0:
                    b_wanted.add(candidate + delta)

    a_rows, a_row_count = collect_rows(a["bbv"], a_wanted)
    b_rows, b_row_count = collect_rows(b["bbv"], b_wanted)
    regions = []
    for point, cluster, center, candidates in plans:
        if point not in a_rows:
            raise ValueError(f"{workload}: {source_side} point {point} exceeds {a_row_count} BBV rows")
        scored = []
        for candidate in candidates:
            if candidate not in b_rows:
                continue
            interval_score = overlap(a_rows[point], b_rows[candidate])
            context_scores = []
            for delta in range(-context, context + 1):
                if delta == 0:
                    continue
                if point + delta in a_rows and candidate + delta in b_rows:
                    context_scores.append(overlap(a_rows[point + delta], b_rows[candidate + delta]))
            context_score = sum(context_scores) / len(context_scores) if context_scores else interval_score
            score = 0.7 * interval_score + 0.3 * context_score
            scored.append({
                "target_point_b": candidate,
                "target_point": candidate,
                "target_side": target_side,
                "score": score,
                "interval_overlap": interval_score,
                "context_overlap": context_score,
                "source_nonzero_blocks": len(a_rows[point]),
                "target_nonzero_blocks": len(b_rows[candidate]),
                "source_vector_sum": sum(a_rows[point]),
                "target_vector_sum": sum(b_rows[candidate]),
            })
        scored.sort(key=lambda item: (-item["score"], abs(item["target_point_b"] - center)))
        if not scored:
            raise ValueError(f"{workload}: no {target_side} BBV candidates around {source_side} point {point}")
        best = scored[0]
        runner_up_score = scored[1]["score"] if len(scored) > 1 else 0.0
        margin = best["score"] - runner_up_score
        accepted = best["score"] >= min_score and margin >= min_margin
        regions.append({
            "source_point_a": point,
            "source_point": point,
            "source_side": source_side,
            "source_cluster_a": cluster,
            "ratio_search_center_b": center,
            "best": best,
            "runner_up_score": runner_up_score,
            "margin": margin,
            "status": "accepted_experimental" if accepted else "rejected_ambiguous",
            "confidence": "L" if accepted else "R",
            "candidates": scored,
        })

    ordered = sorted(regions, key=lambda item: item["source_point_a"])
    last_target = -1
    for region in ordered:
        target = region["best"]["target_point_b"]
        if region["status"] != "accepted_experimental":
            region["monotonic_with_previous"] = None
            continue
        region["monotonic_with_previous"] = target > last_target
        if not region["monotonic_with_previous"]:
            region["status"] = "rejected_non_monotonic"
            region["confidence"] = "R"
        else:
            last_target = target

    result = {
        "schema_version": 1,
        "workload": workload,
        "source_side": source_side,
        "target_side": target_side,
        "interval_instructions": interval,
        "source_total_instructions": a_total,
        "target_total_instructions": b_total,
        "total_instruction_ratio": ratio,
        "source_bbv_rows": a_row_count,
        "target_bbv_rows": b_row_count,
        "method": "BBV count-multiset overlap with same-offset temporal context",
        "search_radius": radius,
        "context_radius": context,
        "thresholds": {"min_score": min_score, "min_margin": min_margin},
        "semantic_anchor_trace_present": False,
        "production_eligible": False,
        "regions": ordered,
    }
    write_json(suite / "results" / workload / "alignment.json", result)
    return result


def alignment_summary(result: dict) -> dict:
    counts = Counter(region["status"] for region in result["regions"])
    accepted = [region for region in result["regions"] if region["status"] == "accepted_experimental"]
    return {
        "workload": result["workload"],
        "regions": len(result["regions"]),
        "accepted": len(accepted),
        "status_counts": dict(counts),
        "best_accepted_score": max((r["best"]["score"] for r in accepted), default=None),
        "lowest_accepted_target_point": min((r["best"]["target_point_b"] for r in accepted), default=None),
    }


def command_align(args: argparse.Namespace) -> None:
    suite = args.suite.resolve()
    manifest = read_json(suite / "suite-manifest.json")
    source_side, target_side = side_labels(manifest)
    workloads = args.workloads or manifest["workloads"]
    summaries = []
    for workload in workloads:
        result = align_workload(
            suite, workload, source_side, target_side, manifest["interval_instructions"], args.radius,
            args.context, args.min_score, args.min_margin, args.points,
        )
        summaries.append(alignment_summary(result))
    write_json(suite / "results" / "alignment-summary.json", summaries)
    print(json.dumps(summaries, indent=2))


def run_logged(command: list[str], stdout: Path, stderr: Path, timeout: int) -> int:
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("w") as out_handle, stderr.open("w") as err_handle:
        completed = subprocess.run(command, stdout=out_handle, stderr=err_handle, timeout=timeout)
    return completed.returncode


def parse_runtime_log(path: Path) -> dict:
    text = path.read_text(errors="replace")
    checkpoint_count = re.search(r"Taking checkpoint @ instruction count ([0-9]+)", text)
    guest_count = re.search(r"total guest instructions = ([0-9][0-9,]*)", text)
    return {
        "checkpoint_done": "Checkpoint done!" in text,
        "checkpoint_instruction": int(checkpoint_count.group(1)) if checkpoint_count else None,
        "guest_instructions": int(guest_count.group(1).replace(",", "")) if guest_count else None,
        "nemu_good_state": "NEMU exit with good state" in text,
        "hit_good_trap": "HIT GOOD TRAP" in text,
    }


def first_checkpoint(path: Path) -> Path:
    files = sorted(path.glob("**/*_memory_.zstd"))
    if len(files) != 1:
        raise RuntimeError(f"expected one checkpoint below {path}, found {len(files)}")
    return files[0]


def copy_source_checkpoint(
    source_root: Path, suite: Path, workload: str, source_side: str, point: int
) -> Path:
    candidates = sorted((source_root / "checkpoint" / workload / str(point)).glob("*_memory_.zstd"))
    if len(candidates) != 1:
        raise RuntimeError(f"expected one source checkpoint for {workload}:{point}, found {len(candidates)}")
    target = suite / "workloads" / workload / source_side / "checkpoint" / str(point) / candidates[0].name
    copy_file(candidates[0], target)
    return target


def profile_after_restore(
    nemu: Path, gcpt: Path, checkpoint: Path, output: Path, workload: str,
    interval: int, restore_instructions: int, timeout: int,
) -> tuple[Path, dict]:
    log_dir = output / "logs"
    result_dir = output / "profile"
    command = [
        str(nemu), "-b", "-I", str(restore_instructions), "--cpt-restorer", str(gcpt),
        "--simpoint-profile", "--dont-skip-boot", "--cpt-interval", str(interval),
        "-D", str(result_dir), "-w", workload, "-C", "post-restore", str(checkpoint),
    ]
    return_code = run_logged(command, log_dir / "restore.out.log", log_dir / "restore.err.log", timeout)
    stats = parse_runtime_log(log_dir / "restore.out.log")
    stats["return_code"] = return_code
    profile = result_dir / "post-restore" / workload / "simpoint_bbv.gz"
    if return_code != 0 or not stats["nemu_good_state"] or not profile.is_file():
        raise RuntimeError(f"bounded restore/profile failed for {checkpoint}: {stats}")
    return profile, stats


def compare_profiles(a_path: Path, b_path: Path) -> list[dict]:
    def read_all(path: Path) -> list[list[int]]:
        rows = []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                rows.append([int(token.split(":")[-1]) for token in line.split() if token.count(":") >= 2])
        return rows
    a_rows, b_rows = read_all(a_path), read_all(b_path)
    return [
        {
            "window": index,
            "count_multiset_overlap": overlap(a_row, b_row),
            "source_vector_sum": sum(a_row),
            "target_vector_sum": sum(b_row),
            "source_nonzero_blocks": len(a_row),
            "target_nonzero_blocks": len(b_row),
        }
        for index, (a_row, b_row) in enumerate(zip(a_rows, b_rows))
    ]


def command_checkpoint(args: argparse.Namespace) -> None:
    suite = args.suite.resolve()
    manifest = read_json(suite / "suite-manifest.json")
    source_side, target_side = side_labels(manifest)
    interval = manifest["interval_instructions"]
    alignment = read_json(suite / "results" / args.workload / "alignment.json")
    matches = [r for r in alignment["regions"] if r["source_point_a"] == args.source_point]
    if len(matches) != 1:
        raise RuntimeError(
            f"alignment does not contain {args.workload} {source_side} point {args.source_point}"
        )
    region = matches[0]
    if region["status"] != "accepted_experimental" and not args.force:
        raise RuntimeError(f"refusing checkpoint for {region['status']}; use --force only for diagnosis")
    target_point = region["best"]["target_point_b"]
    nemu = suite / "tools" / "riscv64-nemu-interpreter"
    gcpt = suite / "tools" / "gcpt.bin"
    firmware = local_files(suite, args.workload, target_side)["firmware"]
    output = suite / "results" / args.workload / f"{source_side}{args.source_point}-{target_side}{target_point}"
    cluster = output / "target-cluster" / args.workload
    cluster.mkdir(parents=True, exist_ok=True)
    (cluster / "simpoints0").write_text(f"{target_point} 0\n")
    (cluster / "weights0").write_text("1.0 0\n")

    source_checkpoint = copy_source_checkpoint(
        Path(manifest["source_profile_root"]), suite, args.workload, source_side, args.source_point,
    )
    generation_root = output / "target-generation"
    trigger = max(0, target_point * interval - args.warmup)
    max_instructions = args.boot_allowance + trigger + args.generation_tail
    generation_command = [
        str(nemu), str(firmware), "-D", str(generation_root), "-w", args.workload,
        "-C", "aligned", "-b", "-I", str(max_instructions), "-S", str(output / "target-cluster"),
        "--cpt-interval", str(interval), "--warmup-interval", str(args.warmup),
        "--checkpoint-format", "zstd",
    ]
    generation_return_code = run_logged(
        generation_command, output / "logs" / "generate.out.log",
        output / "logs" / "generate.err.log", args.timeout,
    )
    generation_stats = parse_runtime_log(output / "logs" / "generate.out.log")
    if generation_return_code != 0 or not generation_stats["checkpoint_done"]:
        raise RuntimeError(f"target checkpoint generation failed: rc={generation_return_code}, {generation_stats}")
    target_checkpoint = first_checkpoint(generation_root)
    subprocess.run(["zstd", "-t", str(target_checkpoint)], check=True, stdout=subprocess.DEVNULL)

    a_profile, a_restore = profile_after_restore(
        nemu, gcpt, source_checkpoint, output / "source-restore", args.workload,
        interval, args.restore_instructions, args.timeout,
    )
    b_profile, b_restore = profile_after_restore(
        nemu, gcpt, target_checkpoint, output / "target-restore", args.workload,
        interval, args.restore_instructions, args.timeout,
    )
    post_restore = compare_profiles(a_profile, b_profile)
    minimum_post_restore_overlap = min(
        (window["count_multiset_overlap"] for window in post_restore), default=0.0
    )
    status = (
        "validated_experimental"
        if minimum_post_restore_overlap >= args.min_post_restore_overlap
        else "rejected_post_restore_divergence"
    )
    result = {
        "schema_version": 1,
        "workload": args.workload,
        "source_side": source_side,
        "target_side": target_side,
        "source_point_a": args.source_point,
        "source_point": args.source_point,
        "target_point_b": target_point,
        "target_point": target_point,
        "alignment": {
            "score": region["best"]["score"],
            "interval_overlap": region["best"]["interval_overlap"],
            "context_overlap": region["best"]["context_overlap"],
            "margin": region["margin"],
            "confidence": region["confidence"],
        },
        "source_checkpoint": {"path": str(source_checkpoint), "sha256": sha256(source_checkpoint)},
        "target_checkpoint": {"path": str(target_checkpoint), "sha256": sha256(target_checkpoint)},
        "generation": {"return_code": generation_return_code, **generation_stats},
        "source_restore": a_restore,
        "target_restore": b_restore,
        "post_restore_windows": post_restore,
        "minimum_post_restore_overlap": minimum_post_restore_overlap,
        "minimum_post_restore_overlap_threshold": args.min_post_restore_overlap,
        "bounded_restore_only": True,
        "workload_terminal_completion": False,
        "production_eligible": False,
        "status": status,
    }
    write_json(output / "checkpoint-result.json", result)
    print(json.dumps(result, indent=2))


def command_report(args: argparse.Namespace) -> None:
    suite = args.suite.resolve()
    manifest = read_json(suite / "suite-manifest.json")
    report = {"schema_version": 1, "workloads": [], "checkpoint_experiments": []}
    for workload in manifest["workloads"]:
        alignment_path = suite / "results" / workload / "alignment.json"
        if alignment_path.is_file():
            report["workloads"].append(alignment_summary(read_json(alignment_path)))
        for result_path in sorted((suite / "results" / workload).glob("*/checkpoint-result.json")):
            result = read_json(result_path)
            overlaps = [window["count_multiset_overlap"] for window in result["post_restore_windows"]]
            report["checkpoint_experiments"].append({
                "workload": workload,
                "source_side": result.get("source_side", "A"),
                "target_side": result.get("target_side", "B"),
                "source_point_a": result["source_point_a"],
                "target_point_b": result["target_point_b"],
                "alignment_score": result["alignment"]["score"],
                "alignment_margin": result["alignment"]["margin"],
                "generation_checkpoint_instruction": result["generation"]["checkpoint_instruction"],
                "source_restore_good_state": result["source_restore"]["nemu_good_state"],
                "target_restore_good_state": result["target_restore"]["nemu_good_state"],
                "post_restore_window_overlaps": overlaps,
                "minimum_post_restore_overlap": min(overlaps) if overlaps else None,
                "status": result["status"],
                "production_eligible": result["production_eligible"],
                "result": str(result_path),
            })
    report["totals"] = {
        "workloads": len(report["workloads"]),
        "regions": sum(item["regions"] for item in report["workloads"]),
        "accepted_experimental": sum(item["accepted"] for item in report["workloads"]),
        "checkpoint_experiments": len(report["checkpoint_experiments"]),
        "validated_experimental": sum(
            item["status"] == "validated_experimental" for item in report["checkpoint_experiments"]
        ),
    }
    write_json(suite / "results" / "suite-report.json", report)
    print(json.dumps(report, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="copy workload inputs into a self-contained suite")
    prepare.add_argument("--suite", type=Path, required=True)
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--target-root", type=Path, required=True)
    prepare.add_argument(
        "--workloads", nargs="+",
        help="workloads to prepare; omit to discover the common workload set",
    )
    prepare.add_argument("--source-side", default="A", help="local label for the source build (default: A)")
    prepare.add_argument("--target-side", default="B", help="local label for the target build (default: B)")
    prepare.add_argument("--nemu", type=Path)
    prepare.add_argument("--gcpt", type=Path)
    prepare.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    prepare.set_defaults(func=command_prepare)

    align = subparsers.add_parser("align", help="map source SimPoints to target interval candidates")
    align.add_argument("--suite", type=Path, required=True)
    align.add_argument("--workloads", nargs="+")
    align.add_argument("--points", nargs="+", type=int)
    align.add_argument("--radius", type=int, default=8)
    align.add_argument("--context", type=int, default=2)
    align.add_argument("--min-score", type=float, default=0.55)
    align.add_argument("--min-margin", type=float, default=0.10)
    align.set_defaults(func=command_align)

    checkpoint = subparsers.add_parser("checkpoint", help="generate and validate one target-native checkpoint")
    checkpoint.add_argument("--suite", type=Path, required=True)
    checkpoint.add_argument("--workload", required=True)
    checkpoint.add_argument("--source-point", type=int, required=True)
    checkpoint.add_argument("--warmup", type=int, default=DEFAULT_INTERVAL)
    checkpoint.add_argument("--boot-allowance", type=int, default=180_000_000)
    checkpoint.add_argument("--generation-tail", type=int, default=20_000_000)
    checkpoint.add_argument("--restore-instructions", type=int, default=40_000_000)
    checkpoint.add_argument("--min-post-restore-overlap", type=float, default=0.5)
    checkpoint.add_argument("--timeout", type=int, default=180)
    checkpoint.add_argument("--force", action="store_true")
    checkpoint.set_defaults(func=command_checkpoint)

    report = subparsers.add_parser("report", help="summarize alignment and checkpoint results")
    report.add_argument("--suite", type=Path, required=True)
    report.set_defaults(func=command_report)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
