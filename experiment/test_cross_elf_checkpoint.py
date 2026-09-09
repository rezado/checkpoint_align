from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .cross_elf_checkpoint import build_checkpoint_correspondence, export_slices, sha256


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


class SliceExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.suite = self.root / "suite"
        simpoints = self.suite / "workloads" / "demo" / "A" / "cluster" / "simpoints0"
        simpoints.parent.mkdir(parents=True)
        simpoints.write_text("7 3\n11 8\n")
        (self.suite / "suite-manifest.json").write_text(json.dumps({
            "interval_instructions": 20_000_000,
            "workloads": ["demo"],
            "sides": {"source": "A", "target": "B"},
        }))
        archives = []
        for point in (9, 13):
            archive = self.root / "generated" / str(point) / f"_{point}_memory_.zstd"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(f"checkpoint-{point}".encode())
            archives.append(archive)
        batch = {
            "workload": "demo",
            "source_checkpoint_count": 2,
            "status": "complete_with_validation_failures",
            "pairs": [
                {
                    "source_point": 7,
                    "target_point": 9,
                    "status": "validated_experimental",
                    "production_eligible": False,
                    "alignment": {"status": "accepted_experimental", "score": 0.8},
                    "target_checkpoint": {"path": str(archives[0]), "sha256": sha256(archives[0])},
                },
                {
                    "source_point": 11,
                    "target_point": 13,
                    "status": "rejected_post_restore_divergence",
                    "production_eligible": False,
                    "alignment": {"status": "rejected_ambiguous", "score": 0.3},
                    "target_checkpoint": {"path": str(archives[1]), "sha256": sha256(archives[1])},
                },
            ],
        }
        batch_path = self.suite / "results" / "demo" / "checkpoint-all-result.json"
        batch_path.parent.mkdir(parents=True)
        batch_path.write_text(json.dumps(batch))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_exports_validated_or_all_materialized_slices_without_weights(self) -> None:
        validated = export_slices(self.suite, "demo", self.root / "validated", False, "symlink")
        self.assertEqual(validated["slice_count"], 1)
        self.assertTrue((self.root / "validated/checkpoint/demo/9/_9_1.000000_memory_.zstd").is_symlink())
        self.assertEqual((self.root / "validated/cluster/demo/simpoints0").read_text(), "9 3\n")
        self.assertFalse((self.root / "validated/cluster/demo/weights0").exists())

        all_slices = export_slices(self.suite, "demo", self.root / "all", True, "copy")
        self.assertEqual(all_slices["slice_count"], 2)
        self.assertTrue(all_slices["all_source_checkpoints_included"])
        self.assertEqual(
            all_slices["validation_status_counts"],
            {"validated_experimental": 1, "rejected_post_restore_divergence": 1},
        )
        self.assertEqual((self.root / "all/cluster/demo/simpoints0").read_text(), "9 3\n13 8\n")


if __name__ == "__main__":
    unittest.main()
