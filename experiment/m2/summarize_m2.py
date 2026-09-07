#!/usr/bin/env python3
"""Aggregate M2 evidence without promoting unsupported capability tiers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    catalog_path = ROOT / "experiment" / "dwarf-source" / "catalog-summary.json"
    controlled_path = ROOT / "experiment" / "m2" / "results" / "validation-summary.json"
    real_path = ROOT / "experiment" / "m2" / "results" / "libquantum-small" / "validation-summary.json"
    shifted_path = ROOT / "experiment" / "m3" / "shifted-candidate-review.json"
    catalog = read_json(catalog_path)
    controlled = read_json(controlled_path)
    real = read_json(real_path)
    shifted = read_json(shifted_path)

    catalog_hash = catalog.get("manifest", {}).get("sha256")
    catalog_builds = [
        item[side]
        for item in catalog.get("workloads", {}).values()
        for side in ("A", "B")
        if item.get(side, {}).get("status") == "ok"
    ]
    catalog_bound = sum(item.get("manifest_hash") == catalog_hash for item in catalog_builds)
    controlled_m = controlled.get("status") == "passed" and controlled.get("controlled_positive", {}).get("anchor_confidence") == "M"
    real_l = real.get("status") == "passed" and real.get("anchor_confidence") == "L"
    completion_reasons = []
    if not controlled_m:
        completion_reasons.append("CONTROLLED_M_VALIDATION_FAILED")
    completion_reasons.extend(
        [
            "INSUFFICIENT_CONTROLLED_WORKLOADS",
            "NO_HELD_OUT_REAL_HM_ACCEPTED",
            "NO_REFERENCE_INPUT_SHIFTED_RUNTIME_TRACE",
        ]
    )
    report = {
        "schema_version": 1,
        "report_kind": "m2-evidence-summary",
        "status": "partial",
        "static_catalog": {
            "status": "passed" if len(catalog_builds) == 12 and catalog_bound == 12 else "failed",
            "workload_count": len(catalog.get("workloads", {})),
            "build_count": len(catalog_builds),
            "manifest_bound_build_count": catalog_bound,
            "manifest_sha256": catalog_hash,
        },
        "controlled_native": {
            "status": controlled.get("status"),
            "anchor_confidence": controlled.get("controlled_positive", {}).get("anchor_confidence"),
            "identity_exact": controlled.get("metrics", {}).get("identity_exact"),
            "identity_total": controlled.get("metrics", {}).get("identity_total"),
            "correct_accepted": controlled.get("metrics", {}).get("controlled_correct_accepted"),
            "labeled_accepted": controlled.get("metrics", {}).get("controlled_labeled_accepted"),
            "coverage": controlled.get("metrics", {}).get("controlled_coverage"),
            "negative_rejected": controlled.get("metrics", {}).get("negative_rejected"),
        },
        "real_workload_low_confidence": {
            "status": real.get("status"),
            "workload_id": real.get("workload_id"),
            "anchor_confidence": real.get("anchor_confidence"),
            "position_fidelity": real.get("position_fidelity"),
            "validated_event_count": len(real.get("alignment", {}).get("matches", [])) if real_l else 0,
            "functional_output_equal": real.get("functional_output_equal"),
            "global_path_margin": real.get("alignment", {}).get("global_path_margin"),
            "confidence_reason": real.get("confidence_reason"),
        },
        "held_out_real_hm": {
            "status": "not_run",
            "correct_accepted": None,
            "labeled_accepted": None,
            "accepted_precision": None,
            "coverage": None,
        },
        "m3_reference_shifted": {
            "status": shifted.get("status"),
            "candidate_count": shifted.get("method_scope", {}).get("candidate_count"),
            "validated_shifted_pair_count": shifted.get("runtime_validation", {}).get("validated_shifted_pair_count"),
            "catalog_manifest_match": all(
                candidate.get("methods", {}).get("dwarf_source_global", {}).get("catalog_manifest_match") is True
                for candidate in shifted.get("candidates", [])
            ),
        },
        "completion_gate": {
            "met": False,
            "reasons": completion_reasons,
            "next_required_evidence": (
                "Collect complete sparse source/IR marker traces with workload icounts for a real A/B pair; "
                "then validate at least one reference-input shifted candidate independently of BBV."
            ),
        },
        "inputs": {
            str(path.relative_to(ROOT)): sha256_file(path)
            for path in (catalog_path, controlled_path, real_path, shifted_path)
        },
        "weights_used": False,
        "checkpoint_materializer_invoked": False,
        "production_eligible": False,
    }
    output = ROOT / "experiment" / "m2" / "validation-summary.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"], "completion_gate": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
