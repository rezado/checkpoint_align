#!/usr/bin/env python3
"""Validate the real debug-preserving calibration and held-out evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalog_maps(catalog: dict) -> tuple[dict[str, dict], dict[int, dict]]:
    by_id = {anchor["anchor_id"]: anchor for anchor in catalog["anchors"]}
    by_pc: dict[int, dict] = {}
    for anchor in catalog["anchors"]:
        if anchor["kind"] != "function" or anchor["confidence"] != "M":
            continue
        if not anchor.get("source") or not anchor.get("name"):
            continue
        for region in anchor["ranges"]:
            by_pc[region["start"]] = anchor
    return by_id, by_pc


def source_key(anchor: dict) -> tuple[str, int, int | None, str]:
    source = anchor["source"]
    return Path(source["path"]).name, source["line"], source.get("column"), anchor["name"]


def qemu_events(root: Path, by_id: dict[str, dict]) -> list[dict]:
    trace = load(root / "qemu-small-context" / "occurrences.json")
    result = []
    for event in trace["events"]:
        anchor = by_id[event["anchor_id"]]
        result.append(
            {
                "key": source_key(anchor),
                "occurrence": event["occurrence"],
                "pc": event["pc"],
            }
        )
    return result


def nemu_context(hit: dict, by_pc: dict[int, dict]) -> list[dict]:
    result = []
    for event in hit["context"]:
        anchor = by_pc[event["pc"]]
        result.append(
            {
                "key": source_key(anchor),
                "occurrence": event["occurrence"],
                "pc": event["pc"],
            }
        )
    return result


def canonical(events: list[dict], with_occurrence: bool = True) -> list[tuple]:
    if with_occurrence:
        return [(event["key"], event["occurrence"]) for event in events]
    return [event["key"] for event in events]


def matching_windows(sequence: list[tuple], signature: list[tuple]) -> int:
    return sum(
        sequence[index : index + len(signature)] == signature
        for index in range(len(sequence) - len(signature) + 1)
    )


def checkpoint(root: Path, phase: str) -> Path:
    candidates = list((root / f"nemu-{phase}" / "checkpoints").glob("**/*.zstd"))
    if len(candidates) != 1:
        raise RuntimeError(f"expected one {phase} checkpoint under {root}, got {len(candidates)}")
    subprocess.run(["zstd", "-tq", str(candidates[0])], check=True)
    return candidates[0]


def validate_point(
    phase: str,
    occurrence: int,
    source_root: Path,
    target_root: Path,
    source_events: list[dict],
    target_events: list[dict],
    source_by_pc: dict[int, dict],
    target_by_pc: dict[int, dict],
) -> dict:
    source_hit = load(source_root / f"nemu-{phase}-hit.json")
    target_hit = load(target_root / f"nemu-{phase}-hit.json")
    if source_hit["occurrence"] != occurrence or target_hit["occurrence"] != occurrence:
        raise RuntimeError(f"{phase}: target occurrence mismatch")
    source_index = next(
        i
        for i, event in enumerate(source_events)
        if event["key"][3] == "quantum_sigma_x" and event["occurrence"] == occurrence
    )
    target_index = next(
        i
        for i, event in enumerate(target_events)
        if event["key"][3] == "quantum_sigma_x" and event["occurrence"] == occurrence
    )
    source_context = nemu_context(source_hit, source_by_pc)
    target_context = nemu_context(target_hit, target_by_pc)
    qemu_source_context = source_events[source_index - len(source_context) + 1 : source_index + 1]
    qemu_target_context = target_events[target_index - len(target_context) + 1 : target_index + 1]
    exact_context = canonical(source_context) == canonical(target_context)
    if not (
        exact_context
        and canonical(source_context) == canonical(qemu_source_context)
        and canonical(target_context) == canonical(qemu_target_context)
    ):
        raise RuntimeError(f"{phase}: QEMU/NEMU context mismatch")
    occurrence_signature = canonical(source_context)
    name_signature = canonical(source_context, with_occurrence=False)
    source_canonical = canonical(source_events)
    target_canonical = canonical(target_events)
    source_names = canonical(source_events, with_occurrence=False)
    target_names = canonical(target_events, with_occurrence=False)
    source_checkpoint = checkpoint(source_root, phase)
    target_checkpoint = checkpoint(target_root, phase)
    restore_stdout = (target_root / f"nemu-{phase}" / "restore" / "stdout.log").read_text(errors="replace")
    restore_stderr = (target_root / f"nemu-{phase}" / "restore" / "stderr.log").read_text(errors="replace")
    terminal_ok = (
        "HIT GOOD TRAP" in restore_stdout
        and "NEMU exit with good state" in restore_stdout
        and "20 = 10 * 2" in restore_stderr
        and "END   libquantum" in restore_stderr
    )
    if not terminal_ok:
        raise RuntimeError(f"{phase}: restore did not reach the expected terminal state")
    return {
        "status": "accepted",
        "classification": "exact",
        "source": {
            "pc": source_hit["pc"],
            "occurrence": occurrence,
            "workload_icount": source_hit["workload_icount"],
            "checkpoint": str(source_checkpoint),
            "checkpoint_sha256": sha256(source_checkpoint),
        },
        "target": {
            "pc": target_hit["pc"],
            "occurrence": occurrence,
            "workload_icount": target_hit["workload_icount"],
            "checkpoint": str(target_checkpoint),
            "checkpoint_sha256": sha256(target_checkpoint),
        },
        "pc_delta": target_hit["pc"] - source_hit["pc"],
        "context_events": len(source_context),
        "context_exact_across_builds": exact_context,
        "context_exact_qemu_nemu": True,
        "context_occurrence_window_matches": {
            "source": matching_windows(source_canonical, occurrence_signature),
            "target": matching_windows(target_canonical, occurrence_signature),
        },
        "context_name_only_window_matches": {
            "source": matching_windows(source_names, name_signature),
            "target": matching_windows(target_names, name_signature),
        },
        "checkpoint_zstd_test": "passed",
        "restore_terminal": "HIT GOOD TRAP",
        "restore_workload_output": "20 = 10 * 2",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--nemu", required=True, type=Path)
    parser.add_argument("--restorer", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    target_root = args.target_root.resolve()
    source_catalog = load(source_root / "catalog.json")
    target_catalog = load(target_root / "catalog.json")
    source_build = load(source_root / "build-manifest.json")
    target_build = load(target_root / "build-manifest.json")
    source_firmware = load(source_root / "firmware-small" / "firmware-manifest.json")
    target_firmware = load(target_root / "firmware-small" / "firmware-manifest.json")
    if source_firmware["elf_sha256"] != source_build["elf_sha256"]:
        raise RuntimeError("source firmware does not contain the source ELF")
    if target_firmware["elf_sha256"] != target_build["elf_sha256"]:
        raise RuntimeError("target firmware does not contain the target ELF")
    source_hash = source_build["source_files"]["gates.c"]["sha256"]
    target_hash = target_build["source_files"]["gates.c"]["sha256"]
    if source_hash != target_hash:
        raise RuntimeError("A/B gates.c source hashes differ")
    source_by_id, source_by_pc = catalog_maps(source_catalog)
    target_by_id, target_by_pc = catalog_maps(target_catalog)
    source_events = qemu_events(source_root, source_by_id)
    target_events = qemu_events(target_root, target_by_id)
    if canonical(source_events) != canonical(target_events):
        raise RuntimeError("A/B canonical QEMU traces differ")
    calibration = validate_point(
        "calibration", 0, source_root, target_root, source_events, target_events, source_by_pc, target_by_pc
    )
    heldout = validate_point(
        "heldout", 1000, source_root, target_root, source_events, target_events, source_by_pc, target_by_pc
    )
    source_reference = load(source_root / "qemu-calibration-complete" / "run-manifest.json")
    target_reference = load(target_root / "qemu-calibration-complete" / "run-manifest.json")
    source_reference_trace = load(source_root / "qemu-calibration-complete" / "occurrences.json")
    target_reference_trace = load(target_root / "qemu-calibration-complete" / "occurrences.json")
    if len(source_reference_trace["events"]) != len(target_reference_trace["events"]):
        raise RuntimeError("reference-input A/B event counts differ")
    if source_reference["runs"][0]["stdout_sha256"] != target_reference["runs"][0]["stdout_sha256"]:
        raise RuntimeError("reference-input A/B functional output differs")
    report = {
        "schema_version": 1,
        "report_kind": "m3-real-debug-occurrence-calibration",
        "status": "accepted",
        "confidence": "M",
        "production_eligible": True,
        "scope": "debug-preserving-validation-builds-only",
        "replaces_reference_artifacts": False,
        "input": {"argv": ["20", "1"], "event_phase": "before_instruction"},
        "policy": {
            "source_identity": ["source_file_sha256", "basename", "line", "column", "function"],
            "dynamic_identity": "zero-based per-anchor occurrence",
            "context": "up to 32 watched events ending at target",
            "acceptance": "exact source identity, occurrence, QEMU/NEMU context, and terminal restore",
        },
        "builds": {
            "source": source_build,
            "target": target_build,
        },
        "runtime_tools": {
            "nemu": {"path": str(args.nemu.resolve()), "sha256": sha256(args.nemu.resolve())},
            "restorer": {
                "path": str(args.restorer.resolve()),
                "sha256": sha256(args.restorer.resolve()),
            },
            "source_firmware": source_firmware,
            "target_firmware": target_firmware,
        },
        "qemu": {
            "event_count": len(source_events),
            "canonical_trace_equal": True,
            "source_manifest": str(source_root / "qemu-small-context" / "run-manifest.json"),
            "target_manifest": str(target_root / "qemu-small-context" / "run-manifest.json"),
        },
        "reference_input_probe": {
            "argv": ["1397", "8"],
            "status": "accepted",
            "event_count": len(source_reference_trace["events"]),
            "source_pc": source_reference_trace["events"][0]["pc"],
            "target_pc": target_reference_trace["events"][0]["pc"],
            "pc_delta": target_reference_trace["events"][0]["pc"]
            - source_reference_trace["events"][0]["pc"],
            "functional_output_equal": True,
            "source_manifest": str(source_root / "qemu-calibration-complete" / "run-manifest.json"),
            "target_manifest": str(target_root / "qemu-calibration-complete" / "run-manifest.json"),
        },
        "calibration": calibration,
        "held_out": heldout,
        "statistics": {"exact": 2, "snapped": 0, "rejected": 0, "wrong_matches": 0},
    }
    args.output.resolve().write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "status": "accepted", "confidence": "M"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
