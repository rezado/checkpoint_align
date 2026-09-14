# A/B checkpoint 精确源位置对齐实现方案

## 1. 目标与结论

本方案只替换当前流程中不可靠的 A source-position binding，保留已经可用的：

- A/B ELF 与运行 manifest 绑定；
- B-native semantic occurrence checkpoint；
- checkpoint sidecar、SHA-256、zstd 完整性检查；
- restore、cross-build、coverage 三类独立验证。

新的核心语义是：

```text
A checkpoint point
  -> A 目标 workload icount
  -> 目标 icount 的真实 before-instruction PC
  -> 覆盖该 PC 的可移植源码 marker
  -> 该 marker 在 A 中的动态 occurrence
  -> B 中同一可移植 marker 的对应 occurrence
  -> B-native checkpoint
```

不能解析为唯一 A/B marker、距离超过限制或映射后发生目标碰撞的 point 必须拒绝，不能再退回到很早以前的任意 watch event。

## 2. 范围

### 2.1 本次实现

- 一次 A 精确 PC 探测，覆盖全部 checkpoint point；
- 一次 A occurrence 探测，只监控第一遍实际选出的少量 marker；
- 使用已有 DWARF/符号 catalog 解析真实 PC；
- 生成现有 `checkpoint-all` 可消费的 request；
- 收紧 B 多目标保存，禁止重复 semantic target 复用旧 hit；
- 为每个 point 输出明确的 fidelity 和 typed rejection；
- 用 mcf 真实 22 点完成 plan、物化、完整性和 restore 验收。

### 2.2 不在本次实现

- 不恢复 BBV/interval 对齐；
- 不把 A/B 相同 icount 当作跨 ELF 坐标；
- 不重新运行 SimPoint 或修改权重；
- 不自动迁移或覆盖 `mcf-semantic-aligned-v2`；
- 不承诺机器指令级 A/B PC 一一对应。跨 ELF 对应粒度以可证明的源码 marker 为准。

## 3. 当前问题

当前 `probe-icounts` 在 A 从头运行时维护函数入口 watch。执行计数越过目标 icount、但当前 PC 不是 watch PC 时，代码把 probe 绑定到 `recent_events.back()`。因此它回答的是：

> 目标边界之前最后一次见到的公共函数入口是什么？

而不是：

> 目标边界时真正执行的 PC 属于哪个语义位置？

mcf point-1822 的目标位置为 36.420B，却被绑定到 4.517B 的 `__printf_buffer_done occurrence=3`，就是这个路径造成的。默认 `--max-snap-instructions=0` 又取消了距离上限，使这种结果仍被标为成功。

## 4. 总体设计

### 4.1 外部 seam

新增一个深 module：

```python
SourceBoundaryResolver.resolve(
    checkpoint_points,
    source_run,
    source_catalog,
    target_catalog,
    policy,
) -> SourceBindingBatch
```

module 内部负责两次 NEMU 探测、PC 范围解析、marker 选择、occurrence 绑定、距离检查和重复检查。调用者只需要理解输入 manifest、policy 和逐点结果。

NEMU 执行由 module 构造时注入的 runner adapter 完成：生产使用 subprocess adapter，测试使用内存 fake adapter。该 seam 有两个真实 adapter，但不暴露更多 NEMU 内部状态。

### 4.2 与现有代码的关系

```text
checkpoint-suite CLI
       |
       v
SourceBoundaryResolver        新增，替换旧 calibrate binding
       |
       +-- A pass 1: exact boundary PC
       +-- DWARF portable marker resolution
       +-- A pass 2: marker occurrence snapshot
       |
       v
requests.json                 保持 checkpoint-all 的输入角色
       |
       v
checkpoint-all
       |
       v
run_nemu_targets()            复用，收紧重复/乱序状态机
       |
       v
B-native checkpoint + sidecar
```

`Position`、`Correspondence` 和 `AlignmentResult` 的公开 interface 暂不扩展。精确边界证据放在独立 artifact 中，并由 `alignment_evidence` 引用；这样不会把 NEMU basic-block 细节泄漏到所有调用者。

## 5. A 第一遍：精确边界 PC

### 5.1 新 NEMU 模式

用新的配置模式替换错误语义，不复用旧缓存：

```text
version 2
mode probe-boundary-pcs
output <exact-boundaries.json>
icount-origin 0
probe <checkpoint_id> <requested_icount>
```

输出示例：

```json
{
  "schema_version": 2,
  "mode": "probe-boundary-pcs",
  "event_phase": "before_instruction",
  "complete": true,
  "probes": [
    {
      "id": 1822,
      "requested_icount": 36420000000,
      "observed_icount": 36420000000,
      "pc": 123456,
      "basic_block_start_pc": 123440,
      "instruction_index": 4,
      "complete": true
    }
  ]
}
```

必须保证：

- `observed_icount == requested_icount`；
- `pc` 表示该 icount 的 before-instruction PC；
- 一次执行可以解析同一 basic block 内的多个 probe；
- 未到达目标、计数域不一致或无法精确定位时输出 incomplete，不允许吸附。

### 5.2 精确 PC 的低开销实现

不要在 280B 指令全程增加昂贵的日志或 Python 回调。当前 PERF_OPT/tcache 已保存每条 Decode 的 `pc` 和 `idx_in_bb`，同一 basic block 的 Decode 连续分配。建议在 `per_bb_profile()` 得到刚完成 basic block 的指令数范围后：

1. 判断下一个 probe icount 是否落在该 block；
2. 从 block 末尾 Decode 和 `idx_in_bb` 定位 block 首 Decode；
3. 由目标 icount 的块内 index 读取对应 Decode 的 PC；
4. 把纯数据传给 semantic-position module 写入结果。

CPU 执行层负责 Decode 到 PC 的定位，semantic-position module 不直接依赖 `Decode`。这保持 seam 清晰，也避免每条指令都执行一次 probe hook。

before-instruction 的 off-by-one 必须通过合成程序验证，不能只依赖公式推断。特别要覆盖压缩指令、普通/跳转 block、同一 block 多 probe 和 batch 结束落在 block 中间。

## 6. 静态解析：真实 PC 到可移植 marker

第一遍完成后，Python 使用现有 `AnchorCatalog` 和 line table，只处理最多 22 个真实 PC。

### 6.1 marker identity

默认 marker 采用：

```text
source path
+ line
+ column/discriminator
+ containing function
+ inline chain
```

得到跨 build 的 `semantic_key`。只使用函数名会把函数内部几十亿条动态指令压缩到一次函数入口，粒度仍然过粗。

### 6.2 选择规则

对每个 A boundary PC：

1. 查找覆盖 PC 的 DWARF line mapping 和 containing function/inline context；
2. 在 B catalog 中查找相同 semantic key；
3. 要求 A marker entry 唯一、B marker entry 唯一；
4. 记录 A/B marker PC、地址范围和 catalog hash；
5. 默认不回退到普通 symbol name；没有唯一源码 marker 时拒绝。

typed rejection：

- `NO_SOURCE_MAPPING`：A PC 没有 DWARF/source mapping；
- `NO_PORTABLE_MARKER`：B 没有相同 semantic key；
- `AMBIGUOUS_SOURCE_MARKER`：A 有多个不可判定 marker；
- `AMBIGUOUS_TARGET_MARKER`：B 有多个不可判定 marker；
- `UNSUPPORTED_INLINE_CONTEXT`：inline chain 无法稳定对应。

如确实需要函数级 fallback，应由后续显式 policy 开启，并把结果标为 `function_entry_experimental`；不作为本次默认路径。

## 7. A 第二遍：绑定 occurrence

第一遍结束后，最多只剩 22 个 source marker，因此第二遍无需记录 6102 个 line entry 或上亿条事件。

新配置：

```text
version 2
mode probe-boundary-occurrences
output <source-occurrences.json>
watch <watch_id> <marker_entry_pc>
probe <checkpoint_id> <requested_icount> <watch_id>
```

运行中为选中的 marker entry 维护 occurrence counter 和 last-hit icount。到达每个目标边界时记录：

```json
{
  "id": 1822,
  "requested_icount": 36420000000,
  "boundary_pc": 123456,
  "watch_id": 3,
  "marker_pc": 123440,
  "occurrence": 27,
  "marker_hit_icount": 36419999996,
  "marker_delta_instructions": -4,
  "complete": true
}
```

约束：

- 第二遍 `boundary_pc` 必须与第一遍相同，否则 `NONDETERMINISTIC_SOURCE_RUN`；
- boundary PC 必须仍位于所选 marker 的 A 地址范围；
- counter 必须至少命中过一次，否则 `MARKER_NOT_ENTERED`；
- `abs(marker_delta_instructions)` 默认不得超过一个 interval；
- 默认阈值为 `interval_instructions`，不能再用 `0` 表示无限制；
- 结果必须记录 marker 粒度，距离只是 fidelity 证据，不能代替结构对应。

## 8. request 与 artifact schema

新增两个权威中间文件：

```text
boundary-probe/exact-boundaries.json
occurrence-probe/source-occurrences.json
```

每个 request 保留现有 `source_position` 和 `target_position`，并增加最小证据引用：

```json
{
  "id": "point-1822",
  "checkpoint_id": 1822,
  "source_checkpoint": "/path/to/A/checkpoint",
  "source_position": {
    "build_id": "A",
    "anchor_id": "<A marker id>",
    "occurrence": 27,
    "pc": 123440,
    "workload_icount": 36419999996,
    "requested_icount": 36420000000,
    "actual_delta_instructions": -4
  },
  "target_position": {
    "build_id": "B",
    "anchor_id": "<B marker id>",
    "occurrence": 27,
    "pc": 123200
  },
  "position_status": "snapped",
  "alignment_evidence": {
    "method": "exact_boundary_source_marker_occurrence",
    "granularity": "source_line",
    "semantic_key": "...",
    "boundary_pc": 123456,
    "boundary_observation_sha256": "...",
    "occurrence_observation_sha256": "..."
  }
}
```

`Position.pc/workload_icount` 继续表示实际用于 occurrence checkpoint 的 marker hit；原始 boundary PC 单独保存在 evidence 中，避免混淆。

## 9. B 多目标物化收紧

### 9.1 物化前检查

生成配置前检查所有：

```text
(target anchor_id, occurrence)
```

必须唯一。多个 A point 映射到同一 target 时返回 `TARGET_POSITION_COLLISION`，不生成多个貌似不同的文件。需要保留对应关系时，可在报告中记录 rejected point 指向碰撞 point，但不能把它标为 materialized。

### 9.2 NEMU 状态机

当前实现按 request 顺序等待 target，并可能在重复 target 上复用旧 hit。改为：

- 用 `(watch_id, occurrence)` 查找尚未完成的 target；
- 实际命中事件时才设置 `target_hit`；
- checkpoint 完成后清空 hit；
- 不因下一个 occurrence `<= old occurrence` 而立即再次保存；
- 不依赖不同 anchor 在 B 中仍保持 A 的全局先后顺序。

每个 sidecar 的 hit icount 必须等于实际 serializer icount；文件名 icount、hit JSON 和 sidecar 三者必须一致。

## 10. 代码修改清单

### Python

- 新增 `src/checkpoint_align/position_aligner/source_boundary.py`
  - `CheckpointPoint`、`BoundaryPolicy`
  - `SourceBinding`
  - `SourceBindingBatch`
  - `SourceBoundaryResolver`
  - portable marker 索引和 typed rejection
- 修改 `src/checkpoint_align/position_aligner/materialize.py`
  - 写入两个 version-2 NEMU probe 配置；
  - B target 唯一性检查；
  - sidecar icount 一致性检查。
- 修改 `src/checkpoint_align/cli.py`
  - `calibrate-source-checkpoints` 调用 resolver；
  - `checkpoint-suite` 串联两遍 A 和现有 B materialization；
  - 新增 `--max-source-displacement`，默认一个 interval；
  - 删除/停止调用旧 `probe-icounts` binding。
- 修改 `tests/test_progress_alignment.py`
  - 只保留 CLI 编排测试；resolver 行为放在 module interface 测试。
- 新增 `src/checkpoint_align/position_aligner/test_source_boundary.py`
  - 通过 fake runner 覆盖完整 resolver interface。

### NEMU

- 修改 `local-src/NEMU/src/checkpoint/semantic_point.cpp`
  - 新增两个 version-2 probe mode；
  - 精确 boundary observation；
  - 指定 marker 的 counter snapshot；
  - B target 按实际 hit 驱动，不复用旧 hit。
- 修改 `local-src/NEMU/include/checkpoint/semantic_point.h`
  - 只暴露 CPU 执行层所需的最小 C interface。
- 修改 `local-src/NEMU/src/cpu/cpu-exec.c`
  - 在完成 basic block 后向 semantic-position module提交精确 probe PC；
  - 不把 Decode/tcache 类型暴露给 Python 或配置层。

## 11. 分阶段实施

### P0：冻结契约

- 为旧错误行为增加一个必失败回归：目标 36B 时最后 watch 在 4B，结果不得绑定到 4B；
- 冻结 version-2 boundary/occurrence JSON schema；
- 冻结 rejection 名称和 `before_instruction` 定义；
- 明确 checkpoint point 到 requested icount 的公式及 warmup 含义。

完成标准：fixture 能明确区分“目标真实 PC”和“上一个 watch PC”。

### P1：实现 exact boundary probe

- 实现 NEMU `probe-boundary-pcs`；
- 添加 sequential、branch、compressed instruction、same-BB multi-probe fixture；
- 用同一 A firmware 连跑两次，比较 22 个 `(icount, PC)`。

完成标准：所有 complete probe 均满足 observed=requested，重复运行字节级一致。

### P2：实现 SourceBoundaryResolver

- 用 catalog range/line table解析 22 个真实 PC；
- 只选择 A/B 唯一的 source-line marker；
- 输出 resolved/rejected，不进行 B checkpoint；
- 实现 displacement 和 source-run determinism 检查。

完成标准：无任何 point 使用 `recent_events.back()` 或无界 nearest-event fallback。

### P3：实现 occurrence probe

- 实现第二遍指定 marker counter；
- 把结果转换为现有 Position/request；
- 检测 target collision；
- 保持 rejected point 可追踪但不进入物化列表。

完成标准：每个 accepted point 都有 exact boundary PC、唯一 marker、A occurrence 和有界 marker delta。

### P4：收紧 B materialization

- 改为 hit-key 驱动多目标保存；
- 删除重复目标旧 hit 复用；
- 检查 hit/serializer filename/sidecar icount 一致；
- 保留 checkpoint hash 和 zstd 校验。

完成标准：合成重复 target 被拒绝；不同 anchor 的目标即使执行顺序改变也能正确保存。

### P5：mcf 真实验收

- 输出到新目录 `data/results/mcf-semantic-aligned-v3`；
- 先 `--plan-only` 审查 22 点 resolution；
- 只物化 accepted 且 target 唯一的 point；
- 执行 archive、restore 和三类 validation；
- 与 v2 对比 source displacement 和碰撞数，不复用 v2 checkpoint。

## 12. 测试矩阵

| 层级 | 场景 | 预期 |
|---|---|---|
| NEMU | 目标落在普通 basic block 中间 | 返回该指令真实 PC |
| NEMU | 同一 block 内有两个 probe | 两个 PC/icount 均正确 |
| NEMU | 目标位于压缩指令 | PC 步长和 phase 正确 |
| NEMU | 未执行到目标 | incomplete，非 success |
| Resolver | A/B 唯一 source-line marker | resolved |
| Resolver | A 有 line、B 无 line | `NO_PORTABLE_MARKER` |
| Resolver | B 有多个 marker PC | `AMBIGUOUS_TARGET_MARKER` |
| Resolver | 两次 A boundary PC 不同 | `NONDETERMINISTIC_SOURCE_RUN` |
| Resolver | marker delta 超限 | `SOURCE_DISPLACEMENT_EXCEEDED` |
| Batch | 两个 point 得到同一 target | `TARGET_POSITION_COLLISION` |
| B NEMU | 不同 anchor 的命中顺序变化 | 按实际 hit 分别保存 |
| Sidecar | serializer icount 与 hit 不同 | 失败，不写成功 sidecar |
| Restore | accepted checkpoint bounded restore | NEMU good state |
| Validation | 无 terminal/state digest | 保持 `not_run/failed`，不得升级声明 |

测试 seam 以 `SourceBoundaryResolver.resolve()` 为主。旧的浅层 config-writer 测试只保留格式关键断言，不复制 resolver 的行为测试。

## 13. mcf 验收命令

先构建：

```sh
NEMU_HOME=$PWD/local-src/NEMU make -C local-src/NEMU -j4
```

先只生成计划：

```sh
checkpoint-align checkpoint-suite \
  --suite data/mcf-align \
  --workload-id mcf \
  --source-checkpoints /nfs-nvme/home/share/checkpoints_profiles/spec06_gcc16_rv64gcb_260724/checkpoint/mcf \
  --nemu local-src/NEMU/build/riscv64-nemu-interpreter \
  --max-source-displacement 20000000 \
  --plan-only \
  --force-probe \
  --output-dir data/results/mcf-semantic-aligned-v3
```

审查计划：

```sh
jq '{status, counts, rejections: [.items[] | select(.status == "rejected") | {id, reason}]}' \
  data/results/mcf-semantic-aligned-v3/source-resolution.json
```

确认后去掉 `--plan-only` 物化。不要以 `22/22 materialized` 作为唯一成功条件；正确的成功条件是：

```text
requested = resolved + rejected
materialized = resolved
failed = 0
target collisions = 0
```

如果某些 point 缺少唯一 marker，应保留 rejected，而不是扩大距离或退回函数名来凑齐 22 个。

## 14. 验收标准

### 14.1 source binding

- 22 个 A point 全部有 exact boundary observation 或 typed rejection；
- accepted point 的两次 A boundary PC 完全一致；
- accepted point 的真实 PC 位于 source marker 范围；
- source marker 在 B 中唯一对应；
- marker delta 不超过配置上限；
- 不存在 `recent_events.back()` fallback；
- 不存在重复 target。

### 14.2 checkpoint artifact

- accepted point 均生成 B-native archive 和 sidecar；
- hit PC/occurrence/icount、文件名 icount、sidecar 完全一致；
- SHA-256 重算一致；
- `zstd -t` 全部通过；
- accepted archive 全部执行至少 40M bounded restore 并得到 NEMU good state。

### 14.3 声明级别

分别报告：

1. `source_position_resolved`：精确 A PC 和 marker occurrence 已绑定；
2. `b_native_materialized`：B checkpoint 已生成且内容完整；
3. `restore_validated`：B from-scratch 与 restore 的 state digest、后续事件和 terminal 证据一致；
4. `cross_build_validated`：A/B 应用语义状态一致；
5. `coverage_validated`：B 内采样覆盖达到要求。

前两项成功不能自动推出后三项。

## 15. 风险与控制

- **DWARF line 多地址**：默认拒绝歧义，后续需要时再用 inline context 或局部事件序列消歧。
- **编译优化删除源码位置**：返回 `NO_PORTABLE_MARKER`，不使用 raw PC 或 icount 比例替代。
- **A 两次运行不稳定**：比较 boundary PC、stdout/hash 和 terminal；不稳定则整批拒绝。
- **basic-block off-by-one**：用 before-instruction 合成 fixture 和少量 A checkpoint restore 作为 oracle。
- **marker 粒度仍过粗**：记录 marker delta；发生 target collision 时拒绝并升级到更细的 line/inline/loop marker。
- **长时间运行**：两次 A 加一次 B，但每次都是单次全程执行；不需要恢复或从头运行 22 次。

## 16. 最小交付边界

第一版只需要做到：

1. 真实 boundary PC；
2. 唯一 source-line marker；
3. 第二遍 A occurrence；
4. 有界 displacement；
5. 重复 target 拒绝；
6. 复用现有 B-native materialization 和验证。

若这六项完成后 mcf 仍有 rejected point，再根据实际 rejection 决定是否需要 loop、inline 或局部序列消歧。不要在没有真实消费者之前加入更多兼容层或复杂 fallback。

## 17. 首轮执行结果（2026-09-14）

本轮按用户允许缩减 P5，只试跑 point-1、857、1412；实现与合成验收仍覆盖
P0-P4 的完整行为。结果位于 `data/results/mcf-semantic-aligned-v3-smoke/`：

- 三个 exact probe 均 complete 且 `observed_icount == requested_icount`；
- boundary PC 分别为 `0x55558b35c754`、`0x10cc8`、`0x10c70`；
- 三点均为 `NO_SOURCE_MAPPING`，requested=3、resolved=0、rejected=3；
- `checkpoint-all --plan-only` 为 rejected=3、failed=0、materialized=0；
- 因 accepted=0，按本方案没有启动真实 mcf B 物化、归档或 restore；
- v2 曾把这三点都映射成功，其中 point-857/1412 复用同一 target，source
  displacement 分别为 -12,603,406,436 和 -23,703,406,436 指令；v3 不再接受
  这种 fallback，也没有 target collision 流入物化；
- 合成 NEMU fixture 已覆盖精确/压缩/跳转/同 BB 多 probe/未到达/重复确定性，
  并实测乱序 target 在 icount 1 和 6 各自保存、文件名一致、SHA-256 已生成、
  两个 zstd 完整。

拒绝原因来自输入 ELF 的证据边界：应用符号存在，但应用本体源码 DWARF 不在
line table 中；当前 6105 条 line row 来自 libgcc/unwind/soft-fp 等库源码。
因此本轮不增加函数名 fallback。若需要真实 mcf accepted checkpoint，下一步
输入条件是用包含应用源码 DWARF 的 A/B ELF 重新运行，而不是放宽位置约束。
