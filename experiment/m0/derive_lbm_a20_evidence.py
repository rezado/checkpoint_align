#!/usr/bin/env python3
"""Derive M0 repeatability evidence from the checked-in lbm artifacts.

The script is read-only with respect to the existing experiment.  It parses
the saved logs and profiles, computes hashes, and writes one small JSON report
next to this file.  It never starts NEMU and never copies or rewrites a
checkpoint/profile artifact.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any


INTERVAL = 20_000_000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NUMBER = r"([0-9][0-9,]*)"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def artifact(root: Path, relative: str, *, required: bool = True) -> dict[str, Any]:
    path = root / relative
    if not path.is_file():
        if required:
            raise FileNotFoundError(path)
        return {"path": relative, "present": False}
    return {
        "path": relative,
        "present": True,
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def first_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1).replace(",", "")) if match else None


def all_ints(pattern: str, text: str) -> list[int]:
    return [int(value.replace(",", "")) for value in re.findall(pattern, text)]


def first_hex(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def line_number(pattern: str, text: str) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line):
            return number
    return None


def parse_runtime_log(path: Path, root: Path) -> dict[str, Any]:
    text = clean(path.read_text(errors="replace"))
    return {
        "log": rel(root, path),
        "start_profiling_base": first_int(r"Current inst count\s+" + NUMBER, text),
        "checkpoint_trigger_observed": all_ints(
            r"Should take cpt now:\s*" + NUMBER, text
        ),
        "checkpoint_notify_observed": all_ints(
            r"Taking checkpoint @ instruction count\s*" + NUMBER, text
        ),
        "saved_pc": first_hex(r"Have taken checkpoint on pc\s+(0x[0-9a-fA-F]+)", text),
        "guest_instructions": first_int(r"total guest instructions\s*=\s*" + NUMBER, text),
        "simpoint": first_int(r"Simpoint 0:\s*@\s*" + NUMBER, text),
        "build_time": (
            re.search(r"Build time:\s*([^\n]+)", text).group(1).strip()
            if re.search(r"Build time:\s*([^\n]+)", text)
            else None
        ),
        "workload_command": (
            re.search(r"^CMD:\s*(.+)$", text, re.MULTILINE).group(1).strip()
            if re.search(r"^CMD:\s*(.+)$", text, re.MULTILINE)
            else None
        ),
        "workload_md5": (
            re.search(r"^([0-9a-f]{32})\s+\./lbm$", text, re.MULTILINE).group(1)
            if re.search(r"^([0-9a-f]{32})\s+\./lbm$", text, re.MULTILINE)
            else None
        ),
        "checkpoint_done": "Checkpoint done!" in text,
        "rng_seed_patched": "Patched FDT /chosen with rng-seed" in text,
        "kernel_crng_initialized": "random: crng init done" in text,
        "hit_good_trap": "HIT GOOD TRAP" in text,
        "nemu_good_state": "NEMU exit with good state" in text,
        "evidence_lines": {
            "rng_seed_patched": line_number(r"Patched FDT /chosen with rng-seed", text),
            "start_profiling_base": line_number(r"Current inst count", text),
            "checkpoint_trigger_observed": line_number(r"Should take cpt now", text),
            "checkpoint_notify_observed": line_number(r"Taking checkpoint @", text),
            "saved_pc": line_number(r"Have taken checkpoint on pc", text),
            "guest_instructions": line_number(r"total guest instructions", text),
            "nemu_good_state": line_number(r"NEMU exit with good state", text),
        },
    }


def read_profile(path: Path) -> list[list[int]]:
    rows: list[list[int]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            rows.append(
                [
                    int(fields[-1])
                    for token in line.split()
                    if len(fields := token.split(":")) >= 3
                    and fields[-1].isdigit()
                ]
            )
    return rows


def profile_stats(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    rows = read_profile(path)
    return {
        "path": relative,
        "sha256": sha256(path),
        "size": path.stat().st_size,
        "rows": len(rows),
        "row_vector_sums": [sum(row) for row in rows],
        "row_nonzero_blocks": [len(row) for row in rows],
        "vector_instruction_sum": sum(sum(row) for row in rows),
    }


def multiset_overlap(a: list[int], b: list[int]) -> float:
    from collections import Counter

    denominator = max(len(a), len(b))
    return sum((Counter(a) & Counter(b)).values()) / denominator if denominator else 1.0


def compare_profiles(root: Path, source: str, target: str) -> dict[str, Any]:
    source_rows = read_profile(root / source)
    target_rows = read_profile(root / target)
    windows = [
        {
            "window": index,
            "count_multiset_overlap": multiset_overlap(a_row, b_row),
            "source_vector_sum": sum(a_row),
            "target_vector_sum": sum(b_row),
            "source_nonzero_blocks": len(a_row),
            "target_nonzero_blocks": len(b_row),
        }
        for index, (a_row, b_row) in enumerate(zip(source_rows, target_rows))
    ]
    return {
        "source_profile": source,
        "target_profile": target,
        "windows": windows,
        "minimum_overlap": min(
            (row["count_multiset_overlap"] for row in windows), default=None
        ),
    }


def build_side_artifacts(root: Path, base: str) -> dict[str, dict[str, Any]]:
    files = {
        "elf": f"{base}/elf/lbm.elf",
        "firmware": f"{base}/bin/lbm.fw_payload.bin",
        "run_script": f"{base}/cmd/lbm.run.sh",
        "json": f"{base}/json/lbm.json",
        "simpoints": f"{base}/cluster/simpoints0",
        "weights": f"{base}/cluster/weights0",
        "profile": f"{base}/profiling/simpoint_bbv.gz",
    }
    return {name: artifact(root, path) for name, path in files.items()}


def build_run(
    root: Path,
    run_id: str,
    family: str,
    generation_log: str,
    generation_err: str,
    checkpoint: str,
    *,
    source_profile: str | None = None,
    target_profile: str | None = None,
    source_restore_log: str | None = None,
    target_restore_log: str | None = None,
    result_json: str | None = None,
    driver_command: str,
    generation_command: str,
    restore_commands: dict[str, str] | None = None,
) -> dict[str, Any]:
    generation = parse_runtime_log(root / generation_log, root)
    stderr = parse_runtime_log(root / generation_err, root)
    # NEMU diagnostics are split between stdout and stderr.  Keep both log
    # records, while exposing fields that only occur in stderr on the run's
    # primary generation record for easy audit/consumption.
    for key in ("workload_command", "workload_md5"):
        if generation[key] is None:
            generation[key] = stderr[key]
    generation["stderr_log"] = stderr
    checkpoint_artifact = artifact(root, checkpoint)
    run: dict[str, Any] = {
        "run_id": run_id,
        "family": family,
        "driver_command": driver_command,
        "generation_command": generation_command,
        "restore_commands": restore_commands or {},
        "generation": generation,
        "checkpoint": checkpoint_artifact,
        "log_artifacts": {
            "generation_stdout": artifact(root, generation_log),
            "generation_stderr": artifact(root, generation_err),
        },
        "source_restore": (
            parse_runtime_log(root / source_restore_log, root)
            if source_restore_log
            else None
        ),
        "target_restore": (
            parse_runtime_log(root / target_restore_log, root)
            if target_restore_log
            else None
        ),
        "profiles": {},
        "result_json": artifact(root, result_json) if result_json else None,
    }
    if source_restore_log:
        run["log_artifacts"]["source_restore_stdout"] = artifact(root, source_restore_log)
        source_restore_err = source_restore_log.replace(".out.log", ".err.log")
        if (root / source_restore_err).is_file():
            run["log_artifacts"]["source_restore_stderr"] = artifact(root, source_restore_err)
    if target_restore_log:
        run["log_artifacts"]["target_restore_stdout"] = artifact(root, target_restore_log)
        target_restore_err = target_restore_log.replace(".out.log", ".err.log")
        if (root / target_restore_err).is_file():
            run["log_artifacts"]["target_restore_stderr"] = artifact(root, target_restore_err)
    if source_profile and target_profile:
        run["profiles"] = {
            "source": profile_stats(root, source_profile),
            "target": profile_stats(root, target_profile),
            "comparison": compare_profiles(root, source_profile, target_profile),
        }
    if result_json:
        run["result_values"] = json.loads((root / result_json).read_text())
    return run


def source_code_evidence(root: Path) -> dict[str, Any]:
    # This file is outside the experiment directory; hash it without copying it.
    source = Path("/nfs/home/wujiabin/work/260820_NEMU_paper/NEMU/src/monitor/fdt_rng_seed.c")
    if not source.is_file():
        return {"present": False, "path": str(source)}
    return {
        "present": True,
        "path": str(source),
        "sha256": sha256(source),
        "read_host_random_lines": [243, 263],
        "read_host_random_device": "/dev/urandom",
        "seed_size_bytes": 32,
        "insert_lines": [266, 299],
        "call_site_lines": [302, 316],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("lbm-a20-repeatability.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()

    legacy_nemu = "experiment/lbm/tools/riscv64-nemu-interpreter"
    generic_nemu = "experiment/multi-workload/tools/riscv64-nemu-interpreter"
    legacy_gcpt = "experiment/lbm/tools/gcpt.bin"
    generic_gcpt = "experiment/multi-workload/tools/gcpt.bin"
    old_checkpoint = "experiment/lbm/A/checkpoint/20/_20_0.000069_memory_.zstd"
    generic_checkpoint = (
        "experiment/multi-workload/workloads/lbm/A/checkpoint/20/"
        "_20_0.000069_memory_.zstd"
    )

    legacy_generation = (
        "/usr/bin/time -f 'wall=%e exit=%x' timeout 120s stdbuf -oL -eL "
        "/nfs/home/wujiabin/work/260820_NEMU_paper/NEMU/build/"
        "riscv64-nemu-interpreter experiment/lbm/B/bin/lbm.fw_payload.bin "
        "-D experiment/lbm/generated-B-point20-final -w lbm -C aligned-point20 "
        "-b -I 540000000 -S experiment/lbm/B-aligned-cluster "
        "--cpt-interval 20000000 --warmup-interval 20000000 "
        "--checkpoint-format zstd"
    )
    generic_generation = (
        "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/"
        "riscv64-nemu-interpreter "
        "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/workloads/"
        "lbm/B/bin/lbm.fw_payload.bin -D "
        "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/"
        "lbm/A20-B20/target-generation -w lbm -C aligned -b -I 580000000 "
        "-S /nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/"
        "results/lbm/A20-B20/target-cluster --cpt-interval 20000000 "
        "--warmup-interval 20000000 --checkpoint-format zstd"
    )
    legacy_restore = {
        side: (
            "timeout 60s stdbuf -oL -eL experiment/lbm/tools/"
            "riscv64-nemu-interpreter -b -I 40000000 --cpt-restorer "
            "experiment/lbm/tools/gcpt.bin --simpoint-profile --dont-skip-boot "
            "--cpt-interval 20000000 -D experiment/lbm/post-restore-profile-"
            f"{side} -w lbm -C post-restore "
            f"{checkpoint}"
        )
        for side, checkpoint in {
            "A": old_checkpoint,
            "B": "experiment/lbm/generated-B-point20-final/aligned-point20/lbm/20/"
            "_20_1.000000_memory_.zstd",
        }.items()
    }
    generic_restore = {
        "A": (
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/"
            "riscv64-nemu-interpreter -b -I 40000000 --cpt-restorer "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/gcpt.bin "
            "--simpoint-profile --dont-skip-boot --cpt-interval 20000000 -D "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/"
            "lbm/A20-B20/source-restore/profile -w lbm -C post-restore "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/workloads/"
            "lbm/A/checkpoint/20/_20_0.000069_memory_.zstd"
        ),
        "B": (
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/"
            "riscv64-nemu-interpreter -b -I 40000000 --cpt-restorer "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/gcpt.bin "
            "--simpoint-profile --dont-skip-boot --cpt-interval 20000000 -D "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/"
            "lbm/A20-B20/target-restore/profile -w lbm -C post-restore "
            "/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/"
            "lbm/A20-B20/target-generation/aligned/lbm/20/"
            "_20_1.000000_memory_.zstd"
        ),
    }
    legacy_rerun_generation = {
        "legacy-rerun-20260828-125428": (
            "timeout 120s experiment/lbm/tools/riscv64-nemu-interpreter "
            "experiment/lbm/B/bin/lbm.fw_payload.bin -D "
            "experiment/lbm/rerun-20260828-125428 -w lbm -C aligned-point20 -b "
            "-I 540000000 -S experiment/lbm/B-aligned-cluster "
            "--cpt-interval 20000000 --warmup-interval 20000000 "
            "--checkpoint-format zstd > "
            "experiment/lbm/rerun-20260828-125428/generate.out.log 2> "
            "experiment/lbm/rerun-20260828-125428/generate.err.log"
        ),
        "legacy-rerun-20260828-125533": (
            "timeout 120s experiment/lbm/tools/riscv64-nemu-interpreter "
            "experiment/lbm/B/bin/lbm.fw_payload.bin -D "
            "experiment/lbm/rerun-20260828-125533 -w lbm -C aligned-point20 -b "
            "-I 540000000 -S experiment/lbm/B-aligned-cluster "
            "--cpt-interval 20000000 --warmup-interval 20000000 "
            "--checkpoint-format zstd > "
            "experiment/lbm/rerun-20260828-125533/generate.out.log 2> "
            "experiment/lbm/rerun-20260828-125533/generate.err.log"
        ),
    }
    legacy_rerun_restore = {
        run_id: (
            "timeout 60s experiment/lbm/tools/riscv64-nemu-interpreter -b "
            "-I 40000000 --cpt-restorer experiment/lbm/tools/gcpt.bin "
            f"{checkpoint} > experiment/lbm/{run_id}/restore.out.log 2> "
            f"experiment/lbm/{run_id}/restore.err.log"
        )
        for run_id, checkpoint in {
            "legacy-rerun-20260828-125428": (
                "experiment/lbm/rerun-20260828-125428/aligned-point20/lbm/20/"
                "_20_1.000000_memory_.zstd"
            ),
            "legacy-rerun-20260828-125533": (
                "experiment/lbm/rerun-20260828-125533/aligned-point20/lbm/20/"
                "_20_1.000000_memory_.zstd"
            ),
        }.items()
    }

    runs = [
        build_run(
            root,
            "legacy-rerun-20260828-125428",
            "legacy_reproduce_sh",
            "experiment/lbm/rerun-20260828-125428/generate.out.log",
            "experiment/lbm/rerun-20260828-125428/generate.err.log",
            "experiment/lbm/rerun-20260828-125428/aligned-point20/lbm/20/"
            "_20_1.000000_memory_.zstd",
            target_restore_log="experiment/lbm/rerun-20260828-125428/restore.out.log",
            driver_command="./experiment/lbm/reproduce.sh",
            generation_command=legacy_rerun_generation["legacy-rerun-20260828-125428"],
            restore_commands={"target": legacy_rerun_restore["legacy-rerun-20260828-125428"]},
        ),
        build_run(
            root,
            "legacy-rerun-20260828-125533",
            "legacy_reproduce_sh",
            "experiment/lbm/rerun-20260828-125533/generate.out.log",
            "experiment/lbm/rerun-20260828-125533/generate.err.log",
            "experiment/lbm/rerun-20260828-125533/aligned-point20/lbm/20/"
            "_20_1.000000_memory_.zstd",
            target_restore_log="experiment/lbm/rerun-20260828-125533/restore.out.log",
            driver_command="./experiment/lbm/reproduce.sh",
            generation_command=legacy_rerun_generation["legacy-rerun-20260828-125533"],
            restore_commands={"target": legacy_rerun_restore["legacy-rerun-20260828-125533"]},
        ),
        build_run(
            root,
            "legacy-final",
            "legacy_manual",
            "experiment/lbm/restore-logs/B-point20-generate-final.out.log",
            "experiment/lbm/restore-logs/B-point20-generate-final.err.log",
            "experiment/lbm/generated-B-point20-final/aligned-point20/lbm/20/"
            "_20_1.000000_memory_.zstd",
            source_profile="experiment/lbm/post-restore-profile-A/post-restore/lbm/simpoint_bbv.gz",
            target_profile="experiment/lbm/post-restore-profile-B/post-restore/lbm/simpoint_bbv.gz",
            source_restore_log="experiment/lbm/restore-logs/A-point20-post-profile.out.log",
            target_restore_log="experiment/lbm/restore-logs/B-point20-post-profile.out.log",
            result_json="experiment/lbm/experiment-result.json",
            driver_command="manual command recorded in the M0 provenance (see generation/restore commands)",
            generation_command=legacy_generation,
            restore_commands=legacy_restore,
        ),
        build_run(
            root,
            "generic-suite-lbm-A20-B20",
            "cross_elf_checkpoint_cli",
            "experiment/multi-workload/results/lbm/A20-B20/logs/generate.out.log",
            "experiment/multi-workload/results/lbm/A20-B20/logs/generate.err.log",
            "experiment/multi-workload/results/lbm/A20-B20/target-generation/aligned/lbm/20/"
            "_20_1.000000_memory_.zstd",
            source_profile=(
                "experiment/multi-workload/results/lbm/A20-B20/source-restore/profile/"
                "post-restore/lbm/simpoint_bbv.gz"
            ),
            target_profile=(
                "experiment/multi-workload/results/lbm/A20-B20/target-restore/profile/"
                "post-restore/lbm/simpoint_bbv.gz"
            ),
            source_restore_log="experiment/multi-workload/results/lbm/A20-B20/"
            "source-restore/logs/restore.out.log",
            target_restore_log="experiment/multi-workload/results/lbm/A20-B20/"
            "target-restore/logs/restore.out.log",
            result_json="experiment/multi-workload/results/lbm/A20-B20/checkpoint-result.json",
            driver_command=(
                "python3 experiment/cross_elf_checkpoint.py checkpoint --suite "
                "experiment/multi-workload --workload lbm --source-point 20 --timeout 240 "
                "> experiment/multi-workload/results/lbm/checkpoint-command.out.log "
                "2> experiment/multi-workload/results/lbm/checkpoint-command.err.log"
            ),
            generation_command=generic_generation,
            restore_commands=generic_restore,
        ),
    ]

    common_paths = {
        "target_elf": (
            "experiment/lbm/B/elf/lbm.elf",
            "experiment/multi-workload/workloads/lbm/B/elf/lbm.elf",
        ),
        "target_firmware": (
            "experiment/lbm/B/bin/lbm.fw_payload.bin",
            "experiment/multi-workload/workloads/lbm/B/bin/lbm.fw_payload.bin",
        ),
        "target_run_script": (
            "experiment/lbm/B/cmd/lbm.run.sh",
            "experiment/multi-workload/workloads/lbm/B/cmd/lbm.run.sh",
        ),
        "target_json": (
            "experiment/lbm/B/json/lbm.json",
            "experiment/multi-workload/workloads/lbm/B/json/lbm.json",
        ),
        "target_simpoints": (
            "experiment/lbm/B/cluster/simpoints0",
            "experiment/multi-workload/workloads/lbm/B/cluster/simpoints0",
        ),
        "target_weights": (
            "experiment/lbm/B/cluster/weights0",
            "experiment/multi-workload/workloads/lbm/B/cluster/weights0",
        ),
        "target_profile": (
            "experiment/lbm/B/profiling/simpoint_bbv.gz",
            "experiment/multi-workload/workloads/lbm/B/profiling/simpoint_bbv.gz",
        ),
        "source_checkpoint": (old_checkpoint, generic_checkpoint),
        "nemu": (legacy_nemu, generic_nemu),
        "gcpt": (legacy_gcpt, generic_gcpt),
    }
    equivalence: dict[str, Any] = {}
    for name, (left, right) in common_paths.items():
        left_info = artifact(root, left)
        right_info = artifact(root, right)
        equivalence[name] = {
            "left": left_info,
            "right": right_info,
            "byte_identical": left_info.get("sha256") == right_info.get("sha256"),
        }

    report = {
        "schema_version": 1,
        "evidence_kind": "m0-lbm-a20-repeatability",
        "workload": "lbm",
        "interval_contract": {
            "interval_instructions": INTERVAL,
            "interval_index_base": 0,
            "icount_domain": "workload_relative_instructions",
            "event_phase": "before_instruction",
            "observed_trigger_domain": "profiling-relative workload instructions",
            "note": "Existing NEMU checkpoint logs expose trigger overshoot but not a source marker; this report records the observed simulator trigger semantics separately.",
        },
        "baseline_inputs": {
            "source": build_side_artifacts(root, "experiment/lbm/A"),
            "target": build_side_artifacts(root, "experiment/lbm/B"),
            "nemu": artifact(root, legacy_nemu),
            "gcpt": artifact(root, legacy_gcpt),
        },
        "cross_copy_equivalence": equivalence,
        "runs": runs,
        "primary_comparison": {
            "legacy_run_id": "legacy-final",
            "generic_run_id": "generic-suite-lbm-A20-B20",
            "legacy_second_window_overlap": runs[2]["profiles"]["comparison"]["windows"][1]["count_multiset_overlap"],
            "generic_second_window_overlap": runs[3]["profiles"]["comparison"]["windows"][1]["count_multiset_overlap"],
            "target_checkpoint_sha_equal": runs[2]["checkpoint"]["sha256"] == runs[3]["checkpoint"]["sha256"],
            "target_post_profile_sha_equal": runs[2]["profiles"]["target"]["sha256"] == runs[3]["profiles"]["target"]["sha256"],
            "trigger_icount_equal": runs[2]["generation"]["checkpoint_notify_observed"] == runs[3]["generation"]["checkpoint_notify_observed"],
            "saved_pc_equal": runs[2]["generation"]["saved_pc"] == runs[3]["generation"]["saved_pc"],
        },
        "repeatability_summary": {
            "generation_runs": len(runs),
            "runs_with_checkpoint": sum(run["generation"]["checkpoint_done"] for run in runs),
            "unique_trigger_values": sorted(
                {
                    value
                    for run in runs
                    for value in run["generation"]["checkpoint_notify_observed"]
                }
            ),
            "unique_saved_pcs": sorted(
                {
                    run["generation"]["saved_pc"]
                    for run in runs
                    if run["generation"]["saved_pc"] is not None
                }
            ),
            "unique_checkpoint_shas": sorted(
                {run["checkpoint"]["sha256"] for run in runs}
            ),
            "all_generation_runs_have_rng_seed_marker": all(
                run["generation"]["rng_seed_patched"] for run in runs
            ),
            "repeatability_gate": "not_met",
            "gate_reason": "same target bytes and command family still produced different trigger/PC/checkpoint bytes; seed bytes were not recorded",
        },
        "rng_seed_hypothesis": {
            "classification": "leading_hypothesis_not_proven_unique",
            "observed_log_marker": "Patched FDT /chosen with rng-seed",
            "all_runs_observed_marker": all(
                run["generation"]["rng_seed_patched"] for run in runs
            ),
            "source_implementation": source_code_evidence(root),
            "reasoning": "The matching NEMU source reads 32 bytes from /dev/urandom when the DTB lacks rng-seed, then inserts the property. The saved logs do not include the bytes, so the artifacts establish an uncontrolled entropy input and correlation, not exclusivity of causation.",
        },
        "interpretation": {
            "calibration_eligible": False,
            "terminal_completion_proven": False,
            "checkpoint_restore_proven": True,
            "weights_in_alignment_semantics": False,
            "notes": [
                "The profile and checkpoint hashes are provenance, not semantic correspondence labels.",
                "The old and generic runs use the same 20M interval and target cluster bytes; their -I caps differ (540M versus 580M), which changes only the post-trigger stopping point.",
                "No run records a terminal HIT GOOD TRAP after restore; keep this evidence experimental until seed control and semantic markers are added.",
            ],
        },
    }
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["repeatability_summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
