#!/usr/bin/env python3
"""Compare interval BBV count signatures without assuming common block IDs."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path


def read_signatures(path: Path) -> list[list[int]]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            counts = []
            for token in line.split():
                fields = token.split(":")
                if len(fields) >= 3:
                    counts.append(int(fields[-1]))
            rows.append(counts)
    return rows


def multiset_overlap(a: list[int], b: list[int]) -> float:
    denominator = max(len(a), len(b))
    return sum((Counter(a) & Counter(b)).values()) / denominator if denominator else 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, default=Path(__file__).parent)
    parser.add_argument("--source-point", type=int, default=20)
    parser.add_argument("--radius", type=int, default=5)
    args = parser.parse_args()
    root = args.experiment.resolve()
    a_rows = read_signatures(root / "A/profiling/simpoint_bbv.gz")
    b_rows = read_signatures(root / "B/profiling/simpoint_bbv.gz")

    start = max(0, args.source_point - args.radius)
    end = min(len(b_rows), args.source_point + args.radius + 1)
    candidates = [
        {
            "target_point_b": point,
            "count_multiset_overlap": multiset_overlap(a_rows[args.source_point], b_rows[point]),
            "source_nonzero_blocks": len(a_rows[args.source_point]),
            "target_nonzero_blocks": len(b_rows[point]),
            "source_vector_sum": sum(a_rows[args.source_point]),
            "target_vector_sum": sum(b_rows[point]),
        }
        for point in range(start, end)
    ]
    candidates.sort(key=lambda row: row["count_multiset_overlap"], reverse=True)
    result = {
        "method": "bbv-dynamic-count-multiset",
        "source_point_a": args.source_point,
        "search_window_b": [start, end - 1],
        "best": candidates[0],
        "runner_up": candidates[1],
        "neighbor_monotonicity": [
            {
                "source_point_a": point,
                "target_point_b": point,
                "count_multiset_overlap": multiset_overlap(a_rows[point], b_rows[point]),
            }
            for point in range(args.source_point - 2, args.source_point + 3)
        ],
        "confidence": "L",
        "status": "experimental_candidate",
        "limitation": "Block IDs are not statically mapped to common source/IR anchors.",
        "candidates": candidates,
    }
    (root / "bbv-alignment-evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("best", "runner_up", "confidence", "status")}, indent=2))


if __name__ == "__main__":
    main()
