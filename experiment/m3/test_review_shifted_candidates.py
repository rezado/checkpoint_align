from __future__ import annotations

import unittest

from experiment.dwarf_source import OccurrenceEvent, OccurrenceTrace
from experiment.m3.review_shifted_candidates import (
    ARTIFACT_MISMATCH,
    LOW_FIDELITY,
    _dwarf_method,
)


def _trace(build: str, icount: int) -> OccurrenceTrace:
    return OccurrenceTrace(
        build_identity=build,
        events=(OccurrenceEvent("marker", 0, workload_icount=icount, semantic_key="function|marker"),),
    )


def _method(source: OccurrenceTrace, target: OccurrenceTrace):
    return _dwarf_method(
        {"source_interval": 1},
        catalog_available=True,
        catalog_manifest_match=True,
        catalog_manifest_hash="a" * 64,
        suite_manifest_hash="a" * 64,
        trace_pair=(source, target),
        expected_build_identities=("build-A", "build-B"),
    )


class ShiftedDwarfMethodTest(unittest.TestCase):
    def test_exact_icount_and_bound_builds_match(self) -> None:
        result = _method(_trace("build-A", 20_000_000), _trace("build-B", 21_000_000))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["position_fidelity"], "exact")
        self.assertEqual(result["evidence"]["snap_delta_instructions"], 0)

    def test_trace_build_mismatch_is_rejected(self) -> None:
        result = _method(_trace("wrong-A", 20_000_000), _trace("build-B", 21_000_000))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], ARTIFACT_MISMATCH)

    def test_nearest_marker_is_not_promoted_to_exact(self) -> None:
        result = _method(_trace("build-A", 20_000_005), _trace("build-B", 21_000_000))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], LOW_FIDELITY)
        self.assertEqual(result["position_fidelity"], "snapped")
        self.assertEqual(result["evidence"]["snap_delta_instructions"], 5)


if __name__ == "__main__":
    unittest.main()
