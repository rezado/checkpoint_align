from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from .dwarf_source import (
    AddressRange,
    Anchor,
    AnchorCatalog,
    OccurrenceEvent,
    align_occurrence_sequences,
    align_occurrence_sequences_segmented,
    collect_occurrence_trace,
    manifest_sha256,
)


def _catalog() -> AnchorCatalog:
    function = Anchor(
        anchor_id="function:main",
        kind="function",
        name="main",
        linkage_name="main",
        source=None,
        ranges=(AddressRange(0x1000, 0x1010),),
        confidence="M",
        origin="fixture",
    )
    loop = Anchor(
        anchor_id="loop:body",
        kind="loop",
        name="body",
        linkage_name=None,
        source=None,
        ranges=(AddressRange(0x1010, 0x1018),),
        confidence="M",
        origin="fixture",
    )
    return AnchorCatalog(
        elf_path="fixture.elf",
        artifact_sha256="a" * 64,
        build_id=None,
        build_identity="sha256:" + "a" * 64,
        manifest_hash=None,
        architecture="RISC-V",
        text_base=0x1000,
        text_size=0x18,
        anchors=(function, loop),
        pc_map=(),
        line_entries=(),
        source_files=(),
        capabilities={},
        provenance={},
        path_policy={},
    )


class DwarfSourceTest(unittest.TestCase):
    def test_manifest_path_hash_binds_exact_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b'{"b": 2, "a": 1}\n')
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(manifest_sha256(path), expected)
            self.assertNotEqual(manifest_sha256(path), manifest_sha256({"a": 1, "b": 2}))

    def test_collector_counts_function_entries_and_retains_unmatched(self) -> None:
        trace = collect_occurrence_trace(
            _catalog(),
            [0x1000, 0x1000, 0x1234, {"pc": "0x1010", "icount": 42}],
            mode="function-entry",
        )
        self.assertEqual([event.anchor_id for event in trace.events], ["function:main", "function:main"])
        self.assertEqual([event.occurrence for event in trace.events], [0, 1])
        self.assertEqual(trace.unmatched_samples, 2)
        self.assertNotEqual(trace.catalog_sha256, _catalog().artifact_sha256)

    def test_equal_global_paths_are_rejected_as_ambiguous(self) -> None:
        source = [OccurrenceEvent("source", 1, semantic_key="same")]
        target = [
            OccurrenceEvent("target-a", 1, semantic_key="same"),
            OccurrenceEvent("target-b", 1, semantic_key="same"),
        ]
        result = align_occurrence_sequences(source, target)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "AMBIGUOUS")
        self.assertEqual(result.global_path_margin, 0.0)

    def test_controlled_insertion_has_unique_monotonic_path(self) -> None:
        source = [OccurrenceEvent("a", 1, semantic_key="a"), OccurrenceEvent("b", 1, semantic_key="b")]
        target = [
            OccurrenceEvent("a2", 1, semantic_key="a"),
            OccurrenceEvent("extra", 1, semantic_key="extra"),
            OccurrenceEvent("b2", 1, semantic_key="b"),
        ]
        result = align_occurrence_sequences(source, target)
        self.assertEqual(result.status, "matched")
        self.assertEqual([(m.source_index, m.target_index) for m in result.matches], [(0, 0), (1, 2)])
        self.assertEqual(result.target_gaps, (1,))
        self.assertGreater(result.global_path_margin or 0, 0)

    def test_segmented_alignment_uses_unique_sync_anchor(self) -> None:
        source = [
            OccurrenceEvent("a0", 0, semantic_key="repeat"),
            OccurrenceEvent("sync-a", 0, semantic_key="sync"),
            OccurrenceEvent("a1", 1, semantic_key="repeat"),
        ]
        target = [
            OccurrenceEvent("b0", 0, semantic_key="repeat"),
            OccurrenceEvent("extra", 0, semantic_key="extra"),
            OccurrenceEvent("sync-b", 0, semantic_key="sync"),
            OccurrenceEvent("b1", 1, semantic_key="repeat"),
        ]
        result = align_occurrence_sequences_segmented(
            source, target, sync_semantic_keys=("sync",), max_cells=6
        )
        self.assertEqual(result.status, "matched")
        self.assertEqual(
            [(match.source_index, match.target_index) for match in result.matches],
            [(0, 0), (1, 2), (2, 3)],
        )

    def test_segmented_alignment_rejects_without_unique_sync(self) -> None:
        source = [
            OccurrenceEvent("a0", 0, semantic_key="repeat"),
            OccurrenceEvent("a1", 1, semantic_key="repeat"),
        ]
        target = [
            OccurrenceEvent("b0", 0, semantic_key="repeat"),
            OccurrenceEvent("b1", 1, semantic_key="repeat"),
        ]
        result = align_occurrence_sequences_segmented(
            source, target, sync_semantic_keys=("repeat",)
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "NO_SYNC_ANCHOR")

    def test_identical_occurrence_stream_bypasses_dp_budget(self) -> None:
        source = [
            OccurrenceEvent("a", occurrence, semantic_key="repeat")
            for occurrence in range(3)
        ]
        target = [
            OccurrenceEvent("b", occurrence, semantic_key="repeat")
            for occurrence in range(3)
        ]
        result = align_occurrence_sequences_segmented(source, target, max_cells=1)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.diagnostics["method"], "canonical_occurrence_identity")
        self.assertEqual(
            [(match.source_index, match.target_index) for match in result.matches],
            [(0, 0), (1, 1), (2, 2)],
        )


if __name__ == "__main__":
    unittest.main()
