# PositionAligner BBV baseline

This package is the M1 experimental position locator. It is intentionally
separate from `experiment/cross_elf_checkpoint.py` and does not create or
restore checkpoints, read `weights0`, or claim semantic anchor identity.

```python
from experiment.position_aligner import align, build_index

index = build_index([
    {"build_id": "A", "bbv_path": "a/simpoint_bbv.gz",
     "interval_instructions": 20_000_000, "workload_id": "lbm"},
    {"build_id": "B", "bbv_path": "b/simpoint_bbv.gz",
     "interval_instructions": 20_000_000, "workload_id": "lbm"},
], workload={"workload_id": "lbm"})
result = align(index, "A", ["B"], [20, 22])
```

`build_index` parses each BBV once and records build/run manifest hashes,
interval metadata, and per-run resource budgets. `align` scores a ratio-centered
adaptive candidate band, then computes top-two sparse monotonic paths with
source/target gap and warp penalties. Every accepted result remains
`experimental: true`, `confidence_ceiling: "L"`, and `weights_used: false`.

The optional replay command evaluates any prepared suite without modifying it.
It uses `sides.source` and `sides.target` from the manifest, with `A`/`B` as a
backward-compatible default:

```sh
python3 -m experiment.position_aligner replay \
  --suite experiment/multi-workload --output /tmp/m1-bbv-replay.json
```

The checked replay artifact is `m1-replay.json`.  It covers all 209 source
points in the six-workload suite; it is regenerated evidence rather than a
gold-labeled accuracy report.

`SEARCH_TRUNCATED`, `AMBIGUOUS`, `CROSSING`, `INCOMPATIBLE_RUN`, and related
typed reasons are part of the JSON result. BBV interval indices are a coarse
experimental projection (`anchor_id: null`, `position_fidelity: "interpolated"`)
until the DWARF/source occurrence adapter supplies semantic markers.
