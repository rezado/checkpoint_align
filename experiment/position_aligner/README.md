# PositionAligner：动态进度主流程与 BBV 兼容基线

包顶层的 `PositionAligner` 是动态进度深接口。它接收一侧 `BuildRun` 和
稀疏 `ProgressEvent` 序列，返回带 `Correspondence` 的 `AlignmentResult`：

```python
from experiment.position_aligner import PositionAligner

result = PositionAligner().align(
    {"run": run_a, "events": events_a},
    {"run": run_b, "events": events_b},
    correspondence={},
    source_position=position_a,
)
```

位置身份固定为 `(build_id, anchor_id, occurrence, event_phase)`。实现包含
语义候选、显式 gap/occurrence transform、严格单调 top-1/top-2 DP、资源预算
和 typed reject；不读取 SimPoint weight。QEMU-user 事件可以没有 workload
icount，此时只能生成 event-only B target，必须由 NEMU 从 workload 起点命中后
才物化完整 `Position` 和 checkpoint sidecar。

动态编排入口：

```sh
python3 experiment/progress_alignment.py \
  {freeze|collect-events|bind-source|align-progress|materialize-target|validate|report} ...
```

`materialize-target` 只追加 `--semantic-position` 配置并从 B 自身启动；不会
复制 A 的 PC、寄存器、内存或 checkpoint。`validate` 分别输出
`restore.json`、`cross-build.json`、`coverage.json`，三个状态互相独立。

`align`/`build_index` 仍从 `bbv.py` 导出，属于 `bbv_candidate`/interval 兼容
入口；BBV 和总指令比例只能召回、排序和 B 内 coverage，不能产生语义身份证明。

The legacy BBV functions are intentionally separate from
`experiment/cross_elf_checkpoint.py`; they do not create or restore
checkpoints, read `weights0`, or claim semantic anchor identity.

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
