from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .cross_elf_checkpoint import build_checkpoint_correspondence


class CheckpointCorrespondenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.suite = self.root / "suite"
        for point in (1, 7):
            directory = self.source / "checkpoint" / "demo" / str(point)
            directory.mkdir(parents=True)
            (directory / f"_{point}_memory_.zstd").write_bytes(b"checkpoint")
        self.suite.mkdir()
        (self.suite / "suite-manifest.json").write_text(json.dumps({
            "source_profile_root": str(self.source),
            "workloads": ["demo"],
            "sides": {"source": "baseline", "target": "candidate"},
        }))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_alignment(self, points: list[tuple[int, int, str]]) -> None:
        path = self.suite / "results" / "demo" / "alignment.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "regions": [
                {
                    "source_point_a": source,
                    "best": {"target_point_b": target, "score": 0.75},
                    "margin": 0.2,
                    "status": status,
                    "confidence": "L" if status == "accepted_experimental" else "R",
                }
                for source, target, status in points
            ]
        }))

    def test_requires_alignment_for_every_actual_source_checkpoint(self) -> None:
        self.write_alignment([(1, 2, "accepted_experimental")])
        with self.assertRaisesRegex(RuntimeError, "does not cover 1 source checkpoints"):
            build_checkpoint_correspondence(self.suite, "demo")

    def test_maps_every_checkpoint_and_records_target_collisions(self) -> None:
        self.write_alignment([
            (1, 2, "accepted_experimental"),
            (7, 2, "rejected_ambiguous"),
        ])
        result = build_checkpoint_correspondence(self.suite, "demo")
        self.assertTrue(result["all_source_checkpoints_mapped"])
        self.assertEqual(result["mapped_checkpoint_count"], 2)
        self.assertEqual(result["unique_target_point_count"], 1)
        self.assertEqual(result["recommended_mapping_count"], 1)
        self.assertEqual(result["candidate_only_mapping_count"], 1)
        self.assertEqual(result["mappings"][0]["target_collision_sources"], [1, 7])


if __name__ == "__main__":
    unittest.main()
