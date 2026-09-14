# 动态进度跨 ELF checkpoint 对齐指南

当前唯一支持的入口是 `src/checkpoint_align/cli.py`。对齐对象不是
BBV interval、PC 或 BB ID，而是运行时语义位置：

```text
(build_id, anchor_id, occurrence)
```

其中 `anchor_id` 来自源代码/DWARF 或显式运行时 marker，`occurrence` 是该
anchor 在本次运行中的动态出现序号。整个 run 的触发语义固定为
`before_instruction`，只在 run manifest 顶层声明一次。只有目标构建自己的运行事件能够证明
目标位置；静态 catalog 只能用于候选绑定，不能单独产生 checkpoint 身份。

## 流程

```text
freeze -> collect-events -> bind-source -> align-progress
       -> materialize-target -> validate -> report
```

示例：

```sh
checkpoint-align freeze \
  --elf-a /path/to/a.elf --elf-b /path/to/b.elf \
  --run-manifest-a /path/to/a-run.json \
  --run-manifest-b /path/to/b-run.json \
  --output /tmp/mcf-run.json

checkpoint-align collect-events \
  --run /tmp/mcf-run.json --output /tmp/mcf-events.jsonl
checkpoint-align bind-source \
  --run /tmp/mcf-run.json --events /tmp/mcf-events.jsonl \
  --semantic-position /path/to/source-position.json \
  --output /tmp/mcf-bound.json
checkpoint-align align-progress \
  --source /tmp/mcf-bound.json --target-events /tmp/mcf-target-events.jsonl \
  --output /tmp/mcf-alignment.json
checkpoint-align materialize-target \
  --alignment /tmp/mcf-alignment.json --output /tmp/mcf-target.json
checkpoint-align validate \
  --alignment /tmp/mcf-alignment.json --output /tmp/mcf-validation.json
checkpoint-align report \
  --alignment /tmp/mcf-alignment.json --validation /tmp/mcf-validation.json
```

## 结果判定

`align-progress` 为每个 source position 输出一个 typed correspondence。状态
可能是 `MATCHED`、`AMBIGUOUS`、`INCOMPATIBLE_RUN`、`SEARCH_TRUNCATED` 或其他
显式拒绝原因。找到候选不等于置信度通过：只有 score、margin、单调路径和
资源预算都满足时，才会进入 `MATCHED`。

`materialize-target` 仅从 B 自己的 workload 起点运行，并要求生成 B-native
checkpoint 及 sidecar；不会复制 A 的寄存器、内存、PC 或 checkpoint。若 NEMU
正常退出但没有 sidecar，命令仍失败。`validate` 将 restore、cross-build 和
coverage 分开记录，不会把结构完整或短程 restore 自动升级为语义等价。

## mcf 说明

对 mcf 的完整对齐必须先为 22 个 A 动态 occurrence 采集并绑定 source 事件，
再在 B 中逐个搜索同一 semantic anchor 的 occurrence。某个 A checkpoint 即使
已有 B candidate，也只能说明候选被找到；要通过置信度，还需要 B 事件证据、
单调/唯一性检查、目标 checkpoint sidecar 以及 bounded restore/coverage 验证。
任何未满足的项都会保留拒绝状态，而不是伪装成完整对齐。

### 批量恢复

若要把 A 的多个 checkpoint 逐个生成对应的 B checkpoint，使用
`checkpoint-all`，输入一个显式的动态请求列表。每项至少包含唯一 `id` 和
`request`；`request` 沿用 `bind-source` 的 `anchor_id`、`occurrence`、
`occurrence_scope`、`requested_icount` 等字段：

```json
{"checkpoints": [{"id": "point-0", "request": {
  "anchor_id": "symbol:...", "occurrence": 0,
  "occurrence_scope": "global_from_scratch",
  "requested_icount": 20000000,
  "interval_instructions": 20000000
}}]}
```

执行时传入 A/B event envelope 和 NEMU 命令；`--plan-only` 只做绑定与对齐，
`--resume` 复用已存在且位置一致的 sidecar。输出目录按 checkpoint `id`
分开保存，并在每项后更新 `checkpoint-all-result.json`，状态区分
`aligned`、`materialized`、`reused`、`rejected` 和 `failed`。旧 A SimPoint
编号或 checkpoint 文件名没有动态 occurrence，不能自动充当 request。

如果 A checkpoint 目录还没有动态 request，推荐直接运行
`checkpoint-suite`。它为 A/B ELF 建立相同 semantic key 的 watchlist，在一次
从头动态执行中探测 22 个 A 边界，再在一次有序 B 执行中依次保存 22 个
B-native checkpoint。`calibrate-source-checkpoints` 也可以单独运行来只生成
request 文件；它不恢复旧 BBV/interval，也不把 point 编号当作跨 ELF 位置。
精确位置路径会额外生成 `exact-boundaries.json`，再只监控第一遍选出的
portable source marker 并生成 `source-occurrences.json`。没有唯一源码 marker
的点必须保留为 typed rejected，不会回退到函数名或旧的 nearest-event 吸附。
两遍 A 和后续 B 运行都显式传入相同的确定性 `--rng-seed`；seed 会记录在
resolution/calibration evidence 中。探针缓存同时绑定 NEMU、firmware、配置、
最大指令数和 seed，任一项变化都会重新运行。

### 少量 mcf 试跑（2026-09-14）

按用户允许缩减真实验证，只选择 point-1、857、1412，输出位于
`data/results/mcf-semantic-aligned-v3-smoke/`。三点的 exact boundary 均满足
`observed_icount == requested_icount`，PC 分别为 `0x55558b35c754`、`0x10cc8`
和 `0x10c70`。三点随后都以 `NO_SOURCE_MAPPING` 类型化拒绝：当前打包的 mcf
ELF 虽有应用符号，但 line table 只覆盖 libgcc/unwind/soft-fp 等库源码，不能
为这些真实应用边界提供源码 marker。

`checkpoint-all --plan-only` 汇总为 requested=3、rejected=3、failed=0、
materialized=0。按照“只物化 accepted point”的契约，本次没有生成或 restore
真实 mcf B checkpoint；这不是把拒绝隐藏成成功。作为独立实现验证，合成 NEMU
fixture 已验证乱序 hit-key 能分别在 icount 1/6 保存两个归档，文件名与 hit
一致，SHA-256 已生成，且两个归档均通过 `zstd -t`。

## 归档数据

仓库中 `data/results/` 下的旧 BBV/interval 结果和报告仅用于历史
追溯，已删除对应实现与 CLI，不应作为当前流程的输入或完成依据。
