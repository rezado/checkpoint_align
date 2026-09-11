from __future__ import annotations

import unittest

from .progress_alignment import bind_source_position


def event(anchor: str, occurrence: int, pc: int, icount: int) -> dict:
    return {"build_id": "A", "run_id": "A-run", "anchor_id": anchor, "semantic_key": anchor.split(":")[-1], "occurrence": occurrence, "event_phase": "before_instruction", "pc": pc, "workload_icount": icount, "context": {}}


class SourceBindingTest(unittest.TestCase):
    def test_global_from_scratch_occurrence_binds(self) -> None:
        events = [event("A:loop", 0, 10, 100), event("A:loop", 1, 10, 200)]
        result = bind_source_position(events, {"anchor_id": "A:loop", "occurrence": 1, "occurrence_scope": "global_from_scratch", "event_phase": "before_instruction", "requested_icount": 190, "interval_instructions": 100})
        self.assertEqual(result["status"], "snapped")
        self.assertEqual(result["source_position"]["actual_delta_instructions"], 10)

    def test_bare_occurrence_is_not_accepted(self) -> None:
        result = bind_source_position([event("A:loop", 0, 10, 100)], {"anchor_id": "A:loop", "occurrence": 0})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "EVIDENCE_COLLECTION_FAILED")

    def test_repeated_window_is_ambiguous(self) -> None:
        events = [event("A:loop", 0, 10, 100), event("A:loop", 1, 10, 200)]
        result = bind_source_position(events, {"window_events": [event("A:loop", 0, 10, 0)], "source_event_offset": 0})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
