#!/usr/bin/env python3
"""Assemble occurrence-calibration artifacts without fabricating a projection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from experiment.dwarf_source import (
        OccurrenceTrace,
        align_occurrence_sequences_segmented,
        project_event,
    )
except ModuleNotFoundError:  # pragma: no cover - direct path launch
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from experiment.dwarf_source import (
        OccurrenceTrace,
        align_occurrence_sequences_segmented,
        project_event,
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-trace", type=Path, required=True)
    parser.add_argument("--target-trace", type=Path, required=True)
    parser.add_argument("--source-window", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sync-key", action="append", default=[])
    parser.add_argument("--source-index", type=int, default=0)
    parser.add_argument("--max-cells", type=int, default=2_000_000)
    args = parser.parse_args()

    source = OccurrenceTrace.load(args.source_trace)
    target = OccurrenceTrace.load(args.target_trace)
    source_window = read_json(args.source_window)
    alignment = align_occurrence_sequences_segmented(
        source.events,
        target.events,
        sync_semantic_keys=args.sync_key,
        max_cells=args.max_cells,
        source_build=source.build_identity,
        target_build=target.build_identity,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "report_kind": "occurrence-calibration",
        "event_phase": "before_instruction",
        "source_build": source.build_identity,
        "target_build": target.build_identity,
        "source_trace": str(args.source_trace),
        "target_trace": str(args.target_trace),
        "source_window": source_window,
        "alignment": alignment.to_dict(),
        "target_position": None,
        "production_eligible": False,
    }
    if alignment.status == "matched":
        result["target_position"] = project_event(
            args.source_index, source.events, target.events, alignment
        )
    elif not args.sync_key:
        result["alignment"]["reason"] = "NO_SYNC_ANCHOR"
    write_json(args.output, result)
    print(json.dumps({"output": str(args.output), "status": alignment.status, "reason": result["alignment"]["reason"]}))
    return 0 if alignment.status == "matched" else 2


if __name__ == "__main__":
    raise SystemExit(main())
