#!/usr/bin/env python3
"""Collect a sparse real-workload occurrence trace with qemu-user."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from experiment.dwarf_source import (
        align_occurrence_sequences,
        build_catalog,
        canonical_json_hash,
        collect_occurrence_trace,
    )
except ModuleNotFoundError:  # pragma: no cover - direct path launch
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from experiment.dwarf_source import (
        align_occurrence_sequences,
        build_catalog,
        canonical_json_hash,
        collect_occurrence_trace,
    )


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
SUITE = WORKSPACE / "experiment" / "multi-workload"
RESULTS = ROOT / "results" / "libquantum-small"
ARGS = ("20", "1")
SELECTED_FUNCTIONS = {
    "main",
    "quantum_memman",
    "quantum_hadamard.part.0",
    "quantum_swaptheleads",
}
QEMU_TRACE_PC = re.compile(rb"/([0-9a-fA-F]{16})/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def find_qemu() -> Path:
    candidates = [
        os.environ.get("QEMU_RISCV64"),
        shutil.which("qemu-riscv64"),
        "/nfs/home/wujiabin/software/riscv-gcc16/bin/qemu-riscv64",
        "/nfs/home/wujiabin/work/riscv/bin/qemu-riscv64",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("qemu-riscv64 not found; set QEMU_RISCV64")


def qemu_version(qemu: Path) -> str:
    completed = subprocess.run([str(qemu), "--version"], check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout.splitlines()[0]


def selected_entries(catalog: Any) -> dict[int, str]:
    result: dict[int, str] = {}
    for anchor in catalog.anchors:
        if anchor.kind not in {"function", "symbol"} or anchor.name not in SELECTED_FUNCTIONS or not anchor.ranges:
            continue
        result[min(item.start for item in anchor.ranges)] = anchor.anchor_id
    missing = SELECTED_FUNCTIONS - {
        anchor.name
        for anchor in catalog.anchors
        if anchor.ranges and min(item.start for item in anchor.ranges) in result
    }
    if missing:
        raise RuntimeError(f"catalog is missing selected functions: {sorted(missing)}")
    return result


def selected_anchor_payload(catalog: Any, entries: dict[int, str]) -> dict[str, Any]:
    selected_ids = set(entries.values())
    return {
        "schema_version": 1,
        "build_identity": catalog.build_identity,
        "elf_sha256": catalog.artifact_sha256,
        "manifest_hash": catalog.manifest_hash,
        "full_catalog_sha256": canonical_json_hash(catalog.to_dict()),
        "full_catalog_retained": False,
        "anchors": [anchor.to_dict() for anchor in catalog.anchors if anchor.anchor_id in selected_ids],
    }


def extract_samples(trace_path: Path, entries: dict[int, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    with trace_path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            byte_count += len(line)
            line_count += 1
            match = QEMU_TRACE_PC.search(line)
            if match is None:
                continue
            pc = int(match.group(1), 16)
            anchor_id = entries.get(pc)
            if anchor_id is not None:
                samples.append({"pc": pc, "anchor_id": anchor_id, "event_phase": "before_instruction"})
    return samples, {
        "raw_trace_sha256": digest.hexdigest(),
        "raw_trace_bytes": byte_count,
        "raw_trace_lines": line_count,
        "retained_event_count": len(samples),
        "raw_trace_retained": False,
    }


def collect_side(side: str, qemu: Path, temporary: Path) -> dict[str, Any]:
    output_dir = RESULTS / side
    output_dir.mkdir(parents=True, exist_ok=True)
    elf = SUITE / "workloads" / "libquantum" / side / "elf" / "libquantum.elf"
    catalog = build_catalog(elf, manifest=SUITE / "suite-manifest.json", recover_loops=False)
    entries = selected_entries(catalog)
    write_json(output_dir / "selected-anchors.json", selected_anchor_payload(catalog, entries))
    trace_path = temporary / f"{side}.qemu-exec.log"
    stdout_path = output_dir / "stdout.log"
    command = [
        str(qemu),
        "-d",
        "exec,nochain",
        "-D",
        str(trace_path),
        str(elf),
        *ARGS,
    ]
    with stdout_path.open("w", encoding="utf-8") as stdout:
        completed = subprocess.run(command, check=False, stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(f"qemu {side} failed ({completed.returncode}): {completed.stderr}")
    samples, trace_provenance = extract_samples(trace_path, entries)
    occurrence = collect_occurrence_trace(
        catalog,
        samples,
        mode="function-entry",
        event_phase="before_instruction",
        source="qemu-user exec,nochain trace (raw trace not retained)",
    )
    occurrence.metadata.update(
        {
            "collector": "qemu-user-exec-filter-v1",
            "selected_functions": sorted(SELECTED_FUNCTIONS),
            "terminal_observed": True,
            **trace_provenance,
        }
    )
    occurrence_path = output_dir / "occurrences.json"
    occurrence.save(occurrence_path)
    manifest = {
        "schema_version": 1,
        "workload_id": "libquantum-small",
        "source_workload": "SPEC CPU2006 libquantum",
        "build_id": side,
        "input_id": "argv:20,1",
        "functional_path_id": "libquantum-factorization",
        "args": list(ARGS),
        "event_phase": "before_instruction",
        "occurrence_base": 0,
        "elf_sha256": sha256_file(elf),
        "suite_manifest_sha256": sha256_file(SUITE / "suite-manifest.json"),
        "catalog_sha256": canonical_json_hash(catalog.to_dict()),
        "full_catalog_retained": False,
        "qemu_sha256": sha256_file(qemu),
        "qemu_version": qemu_version(qemu),
        "command": command,
        "stdout_sha256": sha256_file(stdout_path),
        "trace_provenance": trace_provenance,
        "selected_functions": sorted(SELECTED_FUNCTIONS),
    }
    write_json(output_dir / "manifest.json", manifest)
    return {"catalog": catalog, "trace": occurrence, "manifest": manifest}


def main() -> int:
    qemu = find_qemu()
    RESULTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="position-align-libquantum-") as directory:
        temporary = Path(directory)
        source = collect_side("A", qemu, temporary)
        target = collect_side("B", qemu, temporary)

    alignment = align_occurrence_sequences(
        source["trace"].events,
        target["trace"].events,
        source_build=source["catalog"].build_identity,
        target_build=target["catalog"].build_identity,
    )
    source_keys = [(event.semantic_key, event.occurrence, event.event_phase) for event in source["trace"].events]
    target_keys = [(event.semantic_key, event.occurrence, event.event_phase) for event in target["trace"].events]
    exact_pairs = [(match.source_index, match.target_index) for match in alignment.matches]
    expected_pairs = [(index, index) for index in range(len(source_keys))]
    output_equal = source["manifest"]["stdout_sha256"] == target["manifest"]["stdout_sha256"]
    exact = source_keys == target_keys and exact_pairs == expected_pairs and alignment.status == "matched"
    report = {
        "schema_version": 1,
        "report_kind": "m2-libquantum-small-qemu-validation",
        "status": "passed" if output_equal and exact else "failed",
        "workload_id": "libquantum-small",
        "args": list(ARGS),
        "source_build": source["catalog"].build_identity,
        "target_build": target["catalog"].build_identity,
        "functional_output_equal": output_equal,
        "source_event_count": len(source_keys),
        "target_event_count": len(target_keys),
        "selected_functions": sorted(SELECTED_FUNCTIONS),
        "alignment": alignment.to_dict(),
        "semantic_occurrence_sequences_equal": source_keys == target_keys,
        "anchor_confidence": "L",
        "position_fidelity": "exact" if exact else "rejected",
        "validation_state": "validated" if exact else "failed",
        "confidence_reason": "Selected workload functions are symbol-only in the supplied ELF catalogs.",
        "runtime_evidence": {
            "independent_trace_present": True,
            "terminal_observed": True,
            "raw_trace_retained": False,
            "source_trace_sha256": source["manifest"]["trace_provenance"]["raw_trace_sha256"],
            "target_trace_sha256": target["manifest"]["trace_provenance"]["raw_trace_sha256"],
        },
        "source_manifest": source["manifest"],
        "target_manifest": target["manifest"],
        "reference_input_shifted_candidate_validated": False,
        "production_eligible": False,
    }
    write_json(RESULTS / "validation-summary.json", report)
    print(json.dumps({"output": str(RESULTS / "validation-summary.json"), "status": report["status"], "events": len(source_keys)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
