#!/usr/bin/env python3
"""Validate the reference-input A28258 to B28259 occurrence checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def canonical(events: list[dict]) -> list[tuple[str, int, str]]:
    return [
        (event["semantic_key"], event["occurrence"], event["event_phase"])
        for event in events
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--source-qemu-dir", required=True, type=Path)
    parser.add_argument("--target-qemu-dir", required=True, type=Path)
    parser.add_argument("--repeat-qemu-dir", required=True, type=Path)
    parser.add_argument("--source-firmware", required=True, type=Path)
    parser.add_argument("--target-firmware", required=True, type=Path)
    parser.add_argument("--nemu", required=True, type=Path)
    parser.add_argument("--restorer", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.candidate_root.resolve()
    source_qemu_dir = args.source_qemu_dir.resolve()
    target_qemu_dir = args.target_qemu_dir.resolve()
    repeat_qemu_dir = args.repeat_qemu_dir.resolve()
    source_trace_path = source_qemu_dir / "occurrences.json"
    target_trace_path = target_qemu_dir / "occurrences.json"
    source_manifest_path = source_qemu_dir / "run-manifest.json"
    target_manifest_path = target_qemu_dir / "run-manifest.json"
    repeat_manifest_path = repeat_qemu_dir / "run-manifest.json"
    source_window_path = root / "reference-A-from-scratch-window.json"
    calibration_path = root / "calibration-summary.json"
    target_position_path = root / "reference-B-target-position.txt"
    target_hit_path = root / "reference-B-target-hit.json"
    restore_context_path = root / "reference-B-restore-context.json"
    target_stderr_path = root / "reference-B-target" / "logs" / "stderr.log"
    restore_stdout_path = root / "reference-B-target" / "restore-terminal" / "stdout.log"
    restore_stderr_path = root / "reference-B-target" / "restore-terminal" / "stderr.log"

    source_trace = load(source_trace_path)
    target_trace = load(target_trace_path)
    source_raw_trace_path = Path(source_trace["source"])
    target_raw_trace_path = Path(target_trace["source"])
    source_watchlist_manifest_path = source_qemu_dir / "watchlist-manifest.json"
    target_watchlist_manifest_path = target_qemu_dir / "watchlist-manifest.json"
    source_events = source_trace["events"]
    target_events = target_trace["events"]
    require(len(source_events) == len(target_events) == 23501, "reference event count mismatch")
    require(canonical(source_events) == canonical(target_events), "A/B canonical occurrence streams differ")

    source_manifest = load(source_manifest_path)
    target_manifest = load(target_manifest_path)
    repeat_manifest = load(repeat_manifest_path)
    for name, manifest in (("source", source_manifest), ("target", target_manifest), ("repeat", repeat_manifest)):
        require(manifest["args"] == ["1397", "8"], f"{name} argv mismatch")
        require(manifest["event_phase"] == "before_instruction", f"{name} event phase mismatch")
        require(all(run["exit_status"] == 0 for run in manifest["runs"]), f"{name} QEMU run failed")
        require(all(run["plugin_status"]["vcpus"] == 1 for run in manifest["runs"]), f"{name} vCPU count mismatch")
        require(all(not run["plugin_status"]["budget_exceeded"] for run in manifest["runs"]), f"{name} trace budget exceeded")
    require(
        len({source_manifest["plugin_sha256"], target_manifest["plugin_sha256"], repeat_manifest["plugin_sha256"]}) == 1,
        "QEMU traces were collected with different plugin binaries",
    )
    require(
        len({source_manifest["qemu_sha256"], target_manifest["qemu_sha256"], repeat_manifest["qemu_sha256"]}) == 1,
        "QEMU traces were collected with different QEMU binaries",
    )
    require(source_manifest["runs"][0]["stdout_sha256"] == target_manifest["runs"][0]["stdout_sha256"], "A/B functional output differs")
    require(repeat_manifest["repeat_count"] >= 2, "reference repeat evidence is missing")
    require(repeat_manifest["normalized_trace_stable"] and repeat_manifest["stdout_stable"], "reference repeat is unstable")
    repeat_hashes = {run["trace_sha256"] for run in repeat_manifest["runs"]}
    require(len(repeat_hashes) == 1, "reference repeat raw trace hashes differ")
    require(next(iter(repeat_hashes)) == source_manifest["runs"][0]["trace_sha256"], "repeat differs from source trace")
    for run in repeat_manifest["runs"]:
        repeat_events = load(repeat_qemu_dir / f"occurrences-{run['run_index']}.json")["events"]
        require(canonical(repeat_events) == canonical(source_events), "repeat canonical occurrence stream differs")

    source_window = load(source_window_path)
    require(source_window["complete"] and len(source_window["events"]) == 1, "source window is incomplete")
    source_hit = source_window["events"][0]
    source_index = source_hit["occurrence"]
    require(source_index == 6594, "unexpected source occurrence")
    require(source_events[source_index]["pc"] == source_hit["pc"], "source NEMU/QEMU PC mismatch")

    calibration = load(calibration_path)
    projected = calibration["target_position"]
    require(calibration["alignment"]["status"] == "matched", "alignment was not accepted")
    require(calibration["alignment"]["diagnostics"]["method"] == "canonical_occurrence_identity", "unexpected alignment method")
    require(projected["status"] == "matched" and projected["position_fidelity"] == "exact", "projection is not exact")
    require(projected["source_index"] == projected["target_index"] == source_index, "projected index mismatch")

    target_hit = load(target_hit_path)
    target_event = target_events[source_index]
    require(target_hit["complete"] and target_hit["event_phase"] == "before_instruction", "target hit is incomplete")
    require(target_hit["occurrence"] == target_event["occurrence"], "target occurrence mismatch")
    require(target_hit["pc"] == target_event["pc"], "target PC mismatch")
    target_context = target_hit["context"]
    qemu_context = target_events[source_index - len(target_context) + 1 : source_index + 1]
    require(
        [(event["occurrence"], event["pc"]) for event in target_context]
        == [(event["occurrence"], event["pc"]) for event in qemu_context],
        "target NEMU/QEMU context mismatch",
    )

    restore_context = load(restore_context_path)
    require(restore_context["complete"] and restore_context["events"], "restore context is incomplete")
    restored = restore_context["events"]
    expected_after = target_events[source_index : source_index + len(restored)]
    require(
        [(source_index + event["occurrence"], event["pc"]) for event in restored]
        == [(event["occurrence"], event["pc"]) for event in expected_after],
        "post-checkpoint occurrence sequence mismatch",
    )

    checkpoints = list((root / "reference-B-target" / "checkpoints").glob("**/*.zstd"))
    require(len(checkpoints) == 1, f"expected one target checkpoint, got {len(checkpoints)}")
    checkpoint = checkpoints[0]
    subprocess.run(["zstd", "-tq", str(checkpoint)], check=True)
    require(str(target_hit["workload_icount"]) in checkpoint.name, "checkpoint name does not bind target icount")

    terminal_stdout = restore_stdout_path.read_text(errors="replace")
    terminal_stderr = restore_stderr_path.read_text(errors="replace").replace("\r", "")
    target_stderr = target_stderr_path.read_text(errors="replace").replace("\r", "")
    qemu_stdout = (target_qemu_dir / "stdout-1.log").read_text(errors="replace").strip()
    require("HIT GOOD TRAP" in terminal_stdout, "terminal restore did not hit GOOD TRAP")
    require("NEMU exit with good state: 2" in terminal_stdout, "terminal restore state is not terminal-good")
    require("======== END   libquantum ========" in terminal_stderr, "workload END marker is missing")
    resumed_output = target_stderr.rstrip() + "\n" + terminal_stderr
    normalized_resumed_output = "\n".join(
        line.strip() for line in resumed_output.splitlines() if line.strip()
    )
    normalized_qemu_output = "\n".join(
        line.strip() for line in qemu_stdout.splitlines() if line.strip()
    )
    require(normalized_qemu_output in normalized_resumed_output, "checkpointed workload output differs from QEMU output")
    instruction_match = re.search(r"total guest instructions = ([0-9,]+)", terminal_stdout)
    require(instruction_match is not None, "terminal restore instruction count is missing")

    report = {
        "schema_version": 1,
        "report_kind": "m3-reference-input-occurrence-calibration",
        "candidate_id": "libquantum:A28258->B28259",
        "status": "accepted",
        "confidence": "L",
        "production_eligible": False,
        "confidence_reason": "reference workload anchor is symbol-only",
        "input": {"argv": ["1397", "8"], "interval_size": 20000000, "event_phase": "before_instruction"},
        "source_binding": {
            "simpoint": 28258,
            "target_workload_icount": 565160000000,
            "anchor": source_events[source_index]["semantic_key"],
            "pc": source_hit["pc"],
            "occurrence": source_index,
            "workload_icount": source_hit["workload_icount"],
            "snap_delta": source_hit["workload_icount"] - 565160000000,
            "method": "NEMU from-scratch sparse window",
        },
        "projection": {
            "status": "matched",
            "position_fidelity": "exact",
            "method": "canonical_occurrence_identity",
            "source_index": source_index,
            "target_index": projected["target_index"],
            "target_pc": target_hit["pc"],
            "target_occurrence": target_hit["occurrence"],
        },
        "target_materialization": {
            "simpoint": target_hit["interval"],
            "offset": target_hit["offset"],
            "workload_icount": target_hit["workload_icount"],
            "context_events": len(target_context),
            "context_exact_qemu_nemu": True,
            "post_checkpoint_events": len(restored),
            "post_checkpoint_sequence_exact": True,
            "restorer_overhead_instructions": restored[0]["workload_icount"],
        },
        "checkpoint": {
            **artifact(checkpoint),
            "size_bytes": checkpoint.stat().st_size,
            "zstd_test": "passed",
            "restore_terminal": "HIT GOOD TRAP",
            "restore_good_state": 2,
            "restore_guest_instructions": int(instruction_match.group(1).replace(",", "")),
            "workload_end_marker": True,
            "functional_output_equal_qemu": True,
        },
        "qemu": {
            "event_count": len(source_events),
            "canonical_stream_equal": True,
            "functional_output_equal": True,
            "repeat_count": repeat_manifest["repeat_count"],
            "repeat_stable": True,
            "qemu_sha256": source_manifest["qemu_sha256"],
            "plugin_sha256": source_manifest["plugin_sha256"],
            "source_elf_sha256": source_manifest["elf_sha256"],
            "target_elf_sha256": target_manifest["elf_sha256"],
            "source_catalog_sha256": source_manifest["catalog_sha256"],
            "target_catalog_sha256": target_manifest["catalog_sha256"],
            "source_watchlist_manifest_sha256": source_manifest["watchlist_manifest_sha256"],
            "target_watchlist_manifest_sha256": target_manifest["watchlist_manifest_sha256"],
            "source_raw_trace_sha256": source_manifest["runs"][-1]["trace_sha256"],
            "target_raw_trace_sha256": target_manifest["runs"][-1]["trace_sha256"],
        },
        "runtime_tools": {
            "nemu": artifact(args.nemu.resolve()),
            "restorer": artifact(args.restorer.resolve()),
            "source_firmware": artifact(args.source_firmware.resolve()),
            "target_firmware": artifact(args.target_firmware.resolve()),
        },
        "evidence": {
            "source_trace": artifact(source_trace_path),
            "target_trace": artifact(target_trace_path),
            "source_raw_trace": artifact(source_raw_trace_path),
            "target_raw_trace": artifact(target_raw_trace_path),
            "source_watchlist_manifest": artifact(source_watchlist_manifest_path),
            "target_watchlist_manifest": artifact(target_watchlist_manifest_path),
            "source_run_manifest": artifact(source_manifest_path),
            "target_run_manifest": artifact(target_manifest_path),
            "repeat_run_manifest": artifact(repeat_manifest_path),
            "source_window": artifact(source_window_path),
            "alignment": artifact(calibration_path),
            "target_position": artifact(target_position_path),
            "target_hit": artifact(target_hit_path),
            "restore_context": artifact(restore_context_path),
            "target_stderr": artifact(target_stderr_path),
            "restore_stdout": artifact(restore_stdout_path),
            "restore_stderr": artifact(restore_stderr_path),
        },
    }
    args.output.resolve().write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": "accepted", "confidence": "L"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
