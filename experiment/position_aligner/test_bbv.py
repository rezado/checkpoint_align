from __future__ import annotations

import gzip
import unittest
from pathlib import Path

from . import (
    AlignmentPolicy,
    INCOMPATIBLE_RUN,
    SEARCH_TRUNCATED,
    align,
    build_index,
    result_schema,
)


def _write_bbv(path: Path, rows: list[list[int]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(" ".join(f"{index}:0:{value}" for index, value in enumerate(row)) + "\n")


def _runs(tmp_path: Path, target_rows: list[list[int]], *, manifest_a=None, manifest_b=None):
    source_rows = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
    source_path = tmp_path / "a.bbv.gz"
    target_path = tmp_path / "b.bbv.gz"
    _write_bbv(source_path, source_rows)
    _write_bbv(target_path, target_rows)
    return [
        {
            "build_id": "A",
            "bbv_path": source_path,
            "interval_instructions": 10,
            "total_instructions": len(source_rows) * 10,
            "workload_id": "fixture",
            "manifest": manifest_a or {"workload_id": "fixture", "input_hash": "same"},
        },
        {
            "build_id": "B",
            "bbv_path": target_path,
            "interval_instructions": 10,
            "total_instructions": len(target_rows) * 10,
            "workload_id": "fixture",
            "manifest": manifest_b or {"workload_id": "fixture", "input_hash": "same"},
        },
    ]


class BbvAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_global_path_handles_inserted_target_interval(self) -> None:
        rows = [[99, 99], [1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
        policy = AlignmentPolicy(
            initial_window=1,
            max_window=4,
            min_score=0.2,
            min_global_margin=0.0,
            source_gap_penalty=0.9,
        )
        index = build_index(_runs(self.root, rows), workload="fixture", policy=policy)
        result = align(index, "A", "B", [1, 2, 3])
        target = result["results"]["B"]
        mapped = [item["target"]["position"]["interval"] for item in target["positions"]]
        self.assertEqual(mapped, [2, 3, 4])
        self.assertEqual(mapped, sorted(set(mapped)))
        self.assertEqual(len(target["top_paths"]), 2)
        self.assertFalse(target["weights_used"])

    def test_edge_candidate_is_typed_search_truncation(self) -> None:
        rows = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
        policy = AlignmentPolicy(
            initial_window=0,
            max_window=0,
            min_score=0.0,
            min_global_margin=0.0,
        )
        index = build_index(_runs(self.root, rows), workload="fixture", policy=policy)
        # The best candidate is exactly at the radius edge and cannot expand.
        result = align(index, "A", "B", [1])
        target = result["results"]["B"]
        self.assertEqual(target["reason"], SEARCH_TRUNCATED)
        self.assertEqual(target["status"], "rejected")

    def test_incompatible_input_is_rejected_before_matching(self) -> None:
        rows = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
        index = build_index(
            _runs(self.root, rows, manifest_b={"workload_id": "fixture", "input_hash": "different"}),
            workload="fixture",
        )
        result = align(index, "A", "B", [1])
        target = result["results"]["B"]
        self.assertEqual(target["reason"], INCOMPATIBLE_RUN)
        self.assertEqual(target["positions"], [])

    def test_trace_budget_is_recorded_and_typed(self) -> None:
        rows = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
        index = build_index(
            _runs(self.root, rows), workload="fixture",
            policy=AlignmentPolicy(max_events=2, max_seconds=10),
        )
        result = align(index, "A", "B", [1])
        self.assertTrue(index.budget["truncated"])
        self.assertEqual(result["results"]["B"]["reason"], SEARCH_TRUNCATED)

    def test_memory_budget_is_recorded_and_typed(self) -> None:
        rows = [[1, 2], [3, 4], [5, 6]]
        index = build_index(
            _runs(self.root, rows), workload="fixture",
            policy=AlignmentPolicy(max_memory_bytes=1, max_seconds=10),
        )
        result = align(index, "A", "B", [1])
        self.assertTrue(index.budget["truncated"])
        self.assertEqual(result["results"]["B"]["reason"], SEARCH_TRUNCATED)

    def test_result_schema_hook_rejects_missing_envelope(self) -> None:
        with self.assertRaises(ValueError):
            result_schema({"schema_version": 1})

    def test_inline_rows_are_accepted_without_profile_file(self) -> None:
        rows = [[1, 2], [3, 4], [5, 6]]
        index = build_index(
            {
                "A": {"events": rows, "interval_instructions": 10, "workload_id": "fixture"},
                "B": {"events": rows, "interval_instructions": 10, "workload_id": "fixture"},
            },
            workload="fixture",
            policy=AlignmentPolicy(min_score=0.1, min_global_margin=0.0),
        )
        result = align(index, "A", "B", [0, 1])
        self.assertEqual(result["results"]["B"]["status"], "matched")
