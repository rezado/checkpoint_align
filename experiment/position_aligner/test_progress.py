from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import BuildRun, Position, PositionAligner, ProgressEvent, validate_events, validate_position, validate_run
from .materialize import write_target
from .validation import validate_coverage, validate_cross_build, validate_restore


SHA_A = "a" * 64
SHA_B = "b" * 64


def run(build: str, *, input_id: str = "input-01") -> BuildRun:
    return BuildRun("fixture", build, f"fixture-{build}-run1", input_id, "default", "workload_relative_instructions", 100, 0, "before_instruction", SHA_A if build == "A" else SHA_B, SHA_A if build == "A" else SHA_B, "HIT GOOD TRAP", True)


def event(build: str, anchor: str, occurrence: int, icount: int, semantic: str | None = None) -> ProgressEvent:
    return ProgressEvent(build, f"fixture-{build}-run1", f"{build}:{anchor}", semantic or anchor, occurrence, "before_instruction", 0x1000 + icount, icount, {})


def position(value: ProgressEvent, requested: int | None = None) -> Position:
    requested = value.workload_icount if requested is None else requested
    assert value.workload_icount is not None
    interval, offset = divmod(value.workload_icount, 100)
    return Position(value.build_id, value.anchor_id, value.occurrence, value.event_phase, value.pc, value.workload_icount, interval, offset, requested, value.workload_icount - requested)


def trace(build: str, events: list[ProgressEvent], **run_args) -> dict:
    return {"run": run(build, **run_args), "events": events, "evidence_kind": "dynamic_execution", "complete": True}


class ProtocolTest(unittest.TestCase):
    def test_build_run_rejects_weight_and_wrong_phase(self) -> None:
        with self.assertRaisesRegex(ValueError, "weights"):
            validate_run({**run("A").to_dict(), "simpoint_weight": 0.5})
        with self.assertRaisesRegex(ValueError, "event phase"):
            BuildRun(**{**run("A").__dict__, "event_phase": "unknown"})

    def test_build_run_rejects_unknown_field_and_missing_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown BuildRun fields"):
            validate_run({**run("A").to_dict(), "confidence": "H"})
        value = run("A").to_dict()
        del value["elf_sha256"]
        with self.assertRaises(TypeError):
            validate_run(value)

    def test_occurrences_are_zero_based_per_anchor(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero-based"):
            validate_events([event("A", "loop", 1, 100)], run("A"))

    def test_progress_event_may_omit_qemu_icount(self) -> None:
        value = ProgressEvent("A", "fixture-A-run1", "A:loop", "loop", 0, "before_instruction", 0x1000, None)
        self.assertIsNone(value.workload_icount)

    def test_position_projection_is_checked_against_run(self) -> None:
        value = position(event("A", "loop", 0, 100))
        with self.assertRaisesRegex(ValueError, "projection"):
            validate_position({**value.to_dict(), "interval": 9}, run("A"))


class DynamicAlignmentTest(unittest.TestCase):
    def test_controlled_insertion_maps_exact_with_occurrence_transform(self) -> None:
        source = [event("A", "start", 0, 100), event("A", "loop", 0, 200), event("A", "end", 0, 300)]
        target = [event("B", "loop", 0, 50), event("B", "start", 0, 110), event("B", "extra", 0, 150), event("B", "loop", 1, 230), event("B", "end", 0, 340)]
        result = PositionAligner().align(trace("A", source), trace("B", target), {}, position(source[1]), {})
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.correspondence.position_status, "exact")
        self.assertEqual(result.correspondence.target.occurrence, 1)
        self.assertEqual(result.correspondence.evidence["occurrence_transform"], 1)
        self.assertEqual(len(result.top_paths), 2)

    def test_equal_global_paths_are_ambiguous(self) -> None:
        source = [event("A", "loop", 0, 100)]
        target = [event("B", "loop", 0, 100), event("B", "loop", 1, 200)]
        result = PositionAligner().align(trace("A", source), trace("B", target), {}, position(source[0]), {})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.correspondence.position_status, "ambiguous")
        self.assertEqual(result.correspondence.reason, "AMBIGUOUS")

    def test_budget_and_missing_evidence_are_distinct(self) -> None:
        source = [event("A", "loop", 0, 100)]
        truncated = PositionAligner().align(trace("A", source), trace("B", [event("B", "loop", 0, 100), event("B", "extra", 0, 200)]), {}, position(source[0]), {"max_cells": 1})
        missing = PositionAligner().align(trace("A", source), trace("B", []), {}, position(source[0]), {})
        self.assertEqual(truncated.correspondence.reason, "SEARCH_TRUNCATED")
        self.assertEqual(missing.correspondence.reason, "EVIDENCE_COLLECTION_FAILED")

    def test_canonical_identity_bypasses_dp_budget(self) -> None:
        source = [event("A", "loop", item, (item + 1) * 100) for item in range(3)]
        target = [event("B", "loop", item, (item + 1) * 110) for item in range(3)]
        result = PositionAligner().align(trace("A", source), trace("B", target), {}, position(source[2]), {"max_cells": 1})
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.diagnostics["method"], "canonical_occurrence_identity")

    def test_incompatible_input_is_rejected(self) -> None:
        source = [event("A", "loop", 0, 100)]
        result = PositionAligner().align(trace("A", source), trace("B", [event("B", "loop", 0, 100)], input_id="different"), {}, position(source[0]), {})
        self.assertEqual(result.correspondence.reason, "INCOMPATIBLE_RUN")

    def test_incompatible_functional_path_is_rejected(self) -> None:
        source = [event("A", "loop", 0, 100)]
        target = trace("B", [event("B", "loop", 0, 100)])
        target["run"] = BuildRun(**{**target["run"].__dict__, "functional_path_id": "other"})
        result = PositionAligner().align(trace("A", source), target, {}, position(source[0]), {})
        self.assertEqual(result.correspondence.reason, "INCOMPATIBLE_RUN")

    def test_deleted_target_event_is_not_forced(self) -> None:
        source = [event("A", "start", 0, 100), event("A", "deleted", 0, 200), event("A", "end", 0, 300)]
        target = [event("B", "start", 0, 100), event("B", "end", 0, 300)]
        result = PositionAligner().align(trace("A", source), trace("B", target), {}, position(source[1]), {})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.correspondence.reason, "NO_CORRESPONDENCE")

    def test_weight_policy_is_forbidden(self) -> None:
        source = [event("A", "loop", 0, 100)]
        with self.assertRaisesRegex(ValueError, "weights"):
            PositionAligner().align(trace("A", source), trace("B", [event("B", "loop", 0, 100)]), {}, position(source[0]), {"weights": [1.0]})

    def test_static_event_list_is_not_dynamic_evidence(self) -> None:
        source = [event("A", "loop", 0, 100)]
        result = PositionAligner().align({"run": run("A"), "events": source}, trace("B", [event("B", "loop", 0, 100)]), {}, position(source[0]), {})
        self.assertEqual(result.correspondence.reason, "EVIDENCE_COLLECTION_FAILED")


class MaterializeTest(unittest.TestCase):
    def test_target_requires_semantic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "pc"):
                write_target(Path(directory) / "target.json", {"build_id": "B", "anchor_id": "B:loop", "occurrence": 0, "event_phase": "before_instruction"}, run_manifest=run("B").to_dict())


class ValidationTest(unittest.TestCase):
    def test_three_validation_dimensions_are_independent(self) -> None:
        event_value = {"anchor_id": "B:loop", "occurrence": 4, "event_phase": "before_instruction"}
        scratch = {"build_id": "B", "target_event": event_value, "state_digest": "same", "subsequent_events": ["done"], "terminal_observed": True}
        restored = {**scratch, "marker_consumed": False}
        self.assertEqual(validate_restore(scratch, restored)["status"], "validated")
        source_progress = {"context": {"work_unit": 4, "subphase": "update"}, "before_marker": "begin", "after_marker": "end", "application_state": "state"}
        self.assertEqual(validate_cross_build(source_progress, dict(source_progress), "exact")["status"], "validated")
        coverage = validate_coverage({"build_id": "B", "phases": ["init", "run"], "features": [1, 2]}, {"build_id": "B", "phases": ["init", "run"], "features": [2]})
        self.assertEqual(coverage["status"], "validated")
        self.assertEqual(coverage["claim_scope"], "B-internal functional behavior coverage only")

    def test_cross_build_divergence_has_typed_reason(self) -> None:
        source = {"context": {"work_unit": 1, "subphase": "a"}, "before_marker": "x", "after_marker": "y", "application_state": "s"}
        target = {**source, "application_state": "different"}
        result = validate_cross_build(source, target, "exact")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "POST_ALIGN_DIVERGENCE")


if __name__ == "__main__":
    unittest.main()
