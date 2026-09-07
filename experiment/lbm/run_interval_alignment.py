#!/usr/bin/env python3
"""Run the first, interval-only cross-ELF alignment experiment.

This deliberately uses total instruction-count scaling as a sanity check. It
does not claim semantic anchor alignment because no A/B anchor trace is present
in the copied inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import gzip
from pathlib import Path


INTERVAL = 20_000_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_id(path: Path) -> str | None:
    try:
        output = subprocess.check_output(["readelf", "-n", str(path)], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"Build ID:\s*([0-9a-f]+)", output)
    return match.group(1) if match else None


def load_json_total(path: Path, workload: str) -> int:
    data = json.loads(path.read_text())
    return int(data[workload]["insts"])


def load_simpoints(path: Path) -> list[tuple[int, int]]:
    points = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        point, cluster = line.split()[:2]
        points.append((int(point), int(cluster)))
    return points


def load_weights(path: Path) -> dict[int, float]:
    weights = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        weight, cluster = line.split()[:2]
        weights[int(cluster)] = float(weight)
    return weights


def bbv_stats(path: Path) -> dict[str, int | bool]:
    rows = 0
    vector_sum = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            rows += 1
            for token in line.split():
                fields = token.split(":")
                if len(fields) >= 3 and fields[-1].isdigit():
                    vector_sum += int(fields[-1])
    return {
        "gzip_valid": True,
        "rows": rows,
        "vector_instruction_sum": vector_sum,
    }


def log_stats(path: Path) -> dict[str, int | bool]:
    text = path.read_text(errors="replace")
    match = re.search(r"total guest instructions\s*=\s*([0-9][0-9,]*)", text)
    return {
        "hit_good_trap": "HIT GOOD TRAP" in text,
        "total_guest_instructions": int(match.group(1).replace(",", "")) if match else None,
    }


def side_stats(root: Path, workload: str) -> dict:
    return {
        "root": str(root.resolve()),
        "elf": {
            "path": str((root / "elf" / f"{workload}.elf").resolve()),
            "sha256": sha256(root / "elf" / f"{workload}.elf"),
            "build_id": build_id(root / "elf" / f"{workload}.elf"),
        },
        "files": {
            name: {
                "path": str((root / rel).resolve()),
                "sha256": sha256(root / rel),
                "size": (root / rel).stat().st_size,
            }
            for name, rel in {
                "firmware": f"bin/{workload}.fw_payload.bin",
                "kernel": f"kernel/{workload}.Image",
                "rootfs": f"rootfs/{workload}.rootfs.cpio",
                "run_script": f"cmd/{workload}.run.sh",
            }.items()
        },
        "json_total_instructions": load_json_total(root / "json" / f"{workload}.json", workload),
        "simpoints": load_simpoints(root / "cluster" / "simpoints0"),
        "weights": load_weights(root / "cluster" / "weights0"),
        "bbv": bbv_stats(root / "profiling" / "simpoint_bbv.gz"),
        "profiling_log": log_stats(root / "logs" / "profiling.out.log"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, default=Path(__file__).parent)
    parser.add_argument("--workload", default="lbm")
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    a = side_stats(experiment / "A", args.workload)
    b = side_stats(experiment / "B", args.workload)

    regions = []
    mapped = []
    collisions: dict[int, list[int]] = {}
    for point, cluster in a["simpoints"]:
        # Integer arithmetic keeps the mapping deterministic across hosts.
        estimated_b_start = point * INTERVAL * b["json_total_instructions"] / a["json_total_instructions"]
        q = int(estimated_b_start // INTERVAL)
        quantized_start = q * INTERVAL
        collisions.setdefault(q, []).append(point)
        row = {
            "source_point_a": point,
            "source_cluster_a": cluster,
            "weight_a": a["weights"].get(cluster),
            "a_interval": [point * INTERVAL, (point + 1) * INTERVAL],
            "target_point_b": q,
            "b_interval_estimate": [quantized_start, quantized_start + INTERVAL],
            "estimated_b_start_icount": estimated_b_start,
            "quantization_error_icount": quantized_start - estimated_b_start,
            "method": "total-icount-ratio-sanity-only",
            "confidence": "R",
            "semantic_anchor_trace_present": False,
            "checkpoint_eligible": False,
            "status": "rejected_without_dynamic_anchors",
        }
        regions.append(row)
        mapped.append((q, cluster))

    collisions = {str(q): points for q, points in collisions.items() if len(points) > 1}
    (experiment / "aligned-regions.json").write_text(json.dumps(regions, indent=2) + "\n")
    (experiment / "B-aligned-simpoints0").write_text(
        "".join(f"{point} {cluster}\n" for point, cluster in mapped)
    )

    manifest = {
        "workload": args.workload,
        "interval_instructions": INTERVAL,
        "icount_domain": "profile JSON total; BBV vector sum recorded separately",
        "alignment_method": "total-icount-ratio sanity check",
        "semantic_status": "not_proven",
        "checkpoint_status": "not_generated",
        "source": a,
        "target": b,
        "mapping": {
            "regions": len(regions),
            "unique_target_intervals": len(set(point for point, _ in mapped)),
            "collisions": collisions,
            "max_abs_quantization_error_icount": max(
                (abs(row["quantization_error_icount"]) for row in regions), default=0
            ),
        },
    }
    (experiment / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["mapping"], indent=2))


if __name__ == "__main__":
    main()
