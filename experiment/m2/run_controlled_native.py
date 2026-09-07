#!/usr/bin/env python3
"""Build and validate the short M2 native occurrence-trace workload."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from experiment.dwarf_source import (
        OccurrenceEvent,
        align_occurrence_sequences,
        build_catalog,
        canonical_json_hash,
        collect_occurrence_trace,
    )
except ModuleNotFoundError:  # pragma: no cover - direct path launch
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from experiment.dwarf_source import (
        OccurrenceEvent,
        align_occurrence_sequences,
        build_catalog,
        canonical_json_hash,
        collect_occurrence_trace,
    )


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
SOURCE = ROOT / "controlled_marker.c"
RESULTS = ROOT / "results"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(command: list[str], *, stdout: Path | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=WORKSPACE,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr}")
    if stdout is not None:
        stdout.write_text(completed.stdout, encoding="utf-8")


def build_variant(name: str, flags: list[str]) -> dict[str, Any]:
    compiler = shutil.which("gcc")
    if compiler is None:
        raise RuntimeError("gcc is required for the controlled-native M2 experiment")
    output_dir = RESULTS / name
    output_dir.mkdir(parents=True, exist_ok=True)
    binary = output_dir / "controlled-marker"
    compile_command = [
        compiler,
        "-std=c11",
        "-g",
        "-fno-pie",
        "-no-pie",
        f"-fdebug-prefix-map={WORKSPACE}=.",
        *flags,
        str(SOURCE),
        "-o",
        str(binary),
    ]
    run(compile_command)
    manifest = {
        "schema_version": 1,
        "workload_id": "m2-controlled-native",
        "build_id": name,
        "input_id": "constant-v1",
        "functional_path_id": "marker-sequence-v1",
        "event_phase": "before_instruction",
        "occurrence_base": 0,
        "source_sha256": sha256_file(SOURCE),
        "elf_sha256": sha256_file(binary),
        "compile_command": compile_command,
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    catalog = build_catalog(binary, manifest=manifest_path, path_remaps=((str(WORKSPACE), "."),), recover_loops=False)
    catalog_path = output_dir / "catalog.json"
    catalog.save(catalog_path)

    traces = []
    for run_index in range(2 if name == "A-O0" else 1):
        raw_path = output_dir / f"run-{run_index + 1}.pc.json"
        run([str(binary)], stdout=raw_path)
        samples = json.loads(raw_path.read_text(encoding="utf-8"))
        trace = collect_occurrence_trace(
            catalog,
            samples,
            mode="function-entry",
            event_phase="before_instruction",
            source=str(raw_path.relative_to(WORKSPACE)),
        )
        trace_path = output_dir / f"run-{run_index + 1}.occurrences.json"
        trace.metadata.update({"run_index": run_index + 1, "terminal_observed": True})
        trace.save(trace_path)
        traces.append(trace)
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "catalog": catalog,
        "catalog_path": catalog_path,
        "traces": traces,
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    source = build_variant("A-O0", ["-O0"])
    target = build_variant("B-O2-inserted", ["-O2", "-DINSERT_EXTRA_MARKER=1"])

    identity = align_occurrence_sequences(
        source["traces"][0].events,
        source["traces"][1].events,
        source_build=source["catalog"].build_identity,
        target_build=source["catalog"].build_identity,
    )
    controlled = align_occurrence_sequences(
        source["traces"][0].events,
        target["traces"][0].events,
        source_build=source["catalog"].build_identity,
        target_build=target["catalog"].build_identity,
    )
    ambiguity = align_occurrence_sequences(
        [OccurrenceEvent("source", 0, semantic_key="repeated-phase")],
        [
            OccurrenceEvent("target-0", 0, semantic_key="repeated-phase"),
            OccurrenceEvent("target-1", 1, semantic_key="repeated-phase"),
        ],
    )

    identity_pairs = [(match.source_index, match.target_index) for match in identity.matches]
    controlled_pairs = [(match.source_index, match.target_index) for match in controlled.matches]
    expected_identity = [(index, index) for index in range(5)]
    expected_controlled = [(0, 0), (1, 1), (2, 3), (3, 4), (4, 5)]
    identity_exact = identity.status == "matched" and identity_pairs == expected_identity
    controlled_exact = controlled.status == "matched" and controlled_pairs == expected_controlled
    ambiguity_rejected = ambiguity.status == "rejected" and ambiguity.reason == "AMBIGUOUS"
    occurrence_zero_based = source["traces"][0].events[0].occurrence == 0

    report = {
        "schema_version": 1,
        "report_kind": "m2-controlled-native-validation",
        "workload": "m2-controlled-native",
        "status": "passed" if all((identity_exact, controlled_exact, ambiguity_rejected, occurrence_zero_based)) else "failed",
        "builds": {
            name: {
                "elf_sha256": item["manifest"]["elf_sha256"],
                "manifest_sha256": sha256_file(item["manifest_path"]),
                "catalog_sha256": canonical_json_hash(item["catalog"].to_dict()),
                "build_identity": item["catalog"].build_identity,
                "compile_command": item["manifest"]["compile_command"],
                "event_counts": [len(trace.events) for trace in item["traces"]],
                "unmatched_samples": [trace.unmatched_samples for trace in item["traces"]],
            }
            for name, item in (("A-O0", source), ("B-O2-inserted", target))
        },
        "identity": {
            "status": "passed" if identity_exact else "failed",
            "expected_pairs": expected_identity,
            "observed": identity.to_dict(),
            "rerun_trace_equal": source["traces"][0].to_dict()["events"] == source["traces"][1].to_dict()["events"],
        },
        "controlled_positive": {
            "status": "passed" if controlled_exact else "failed",
            "anchor_confidence": "M",
            "position_fidelity": "exact" if controlled_exact else "rejected",
            "validation_state": "validated" if controlled_exact else "failed",
            "expected_pairs": expected_controlled,
            "observed": controlled.to_dict(),
            "target_insertion_indices": list(controlled.target_gaps),
        },
        "negative": {
            "status": "passed" if ambiguity_rejected else "failed",
            "expected_reason": "AMBIGUOUS",
            "observed": ambiguity.to_dict(),
        },
        "protocol": {
            "event_phase": "before_instruction",
            "occurrence_base": 0,
            "occurrence_zero_based_observed": occurrence_zero_based,
            "weights_used": False,
            "checkpoint_materializer_invoked": False,
        },
        "metrics": {
            "controlled_correct_accepted": len(controlled.matches) if controlled_exact else 0,
            "controlled_labeled_accepted": len(expected_controlled),
            "controlled_coverage": len(controlled.matches) / len(expected_controlled),
            "identity_exact": len(identity.matches) if identity_exact else 0,
            "identity_total": len(expected_identity),
            "negative_rejected": int(ambiguity_rejected),
        },
        "held_out_real_workloads": {
            "status": "not_run",
            "runtime_occurrence_trace_count": 0,
            "accepted_precision": None,
            "coverage": None,
            "reason": "The supplied SPEC artifacts contain static DWARF/BBV evidence but no complete independent runtime occurrence traces.",
        },
        "production_eligible": False,
    }
    write_json(RESULTS / "validation-summary.json", report)
    print(json.dumps({"output": str(RESULTS / "validation-summary.json"), "status": report["status"]}, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
