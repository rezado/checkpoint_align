# 跨配置同 Workload 切片位置对齐：后续实施计划

日期：2026-09-03

## 0. 推荐结论

目标应定义为：给定同一 workload、同一输入在配置 A 中的切片位置 `A1`，在配置 B 的动态执行中找到对应位置 `B1`，并输出可复核证据；找不到唯一对应时返回 `rejected`，不能强行给出一个 point。

推荐采用下面的分层方法：

```text
语义锚点确定“执行到了什么代码”
        +
动态 occurrence 确定“这是第几次执行到这里”
        +
全局单调序列对齐消除重复 phase 的局部歧义
        +
BBV/进度比例负责低成本候选召回和辅助证据
        =
A1 -> B1 或明确拒绝
```

跨配置共享的语义不是 PC、BB ID、SimPoint index 或原始动态指令数，而是各配置内的事件坐标及其对应关系：

```text
Position_X = (build_id_X, anchor_id_X, occurrence_X)
Correspondence(A_event, B_event)
```

`occurrence_X` 是 build X 内的局部计数，不假设 A/B 数值相等；对应关系可以包含 occurrence transform，用来表达循环展开、向量化或插入/删除事件。当前协议在 run manifest 顶层固定触发时刻为 `before_instruction`，不把它重复放入每个位置身份。B 中的 PC、workload icount 和 interval index 都是这个语义位置在配置 B 下的物化结果，不是跨配置身份。

权重不属于该问题。`PositionAligner` 不读取、不映射、不验证 SimPoint 权重，也不承诺选点对 B 的全程序性能具有代表性。

## 1. 问题边界

### 1.1 输入

最小输入为：

- A/B 的 build 和运行 manifest；
- 相同 workload、输入、参数和功能路径；
- A 中一个或多个切片位置；
- A/B 可复现的 profiling evidence；
- 明确的 interval 编号、指令域和边界语义。

若 A1 来自固定长度 SimPoint `p`，interval 长度为 `L_A`，核心待映射对象是该 interval 的起点：

```text
A1.requested_icount = p * L_A
A1.context = [p * L_A, (p + 1) * L_A)
```

这个公式必须通过 A->A self-map 确认零基/一基、触发指令执行前后以及 `icount_domain`。后续 1 个或多个 interval 是消歧和验证上下文，不要求把 A 的固定长度终点强行映射成 B 的同长度终点。这样 interface 与“只找对应位置”的目标一致，同时还能发现起点后立即发生的语义漂移。

### 1.2 输出

每个 A1 只允许两类结果：

```text
matched:  A1 -> B1 + evidence + anchor_confidence
          + position_fidelity + validation_state
rejected: A1 -> gap + typed reason + best candidates
```

匹配成功时同时输出两种 B 坐标：

1. B 内精确位置：`(anchor_id_B, occurrence_B)`，以及 A/B 事件对应证据；
2. 兼容现有工具的位置：`target_interval` 和 `offset_in_interval`。

若 B 的 interval 长度为 `L_B`：

```text
target_interval = floor(B_start_icount / L_B)
offset_in_interval = B_start_icount % L_B
```

`target_interval` 只是对精确语义位置的量化。直接从该 interval 起点运行固定 `L_B` 指令，只能称为近似位置对齐。

### 1.3 非目标

- 不复用 A 的 `weights0`，也不计算 B 的权重；
- 不迁移 A 的 PC、寄存器、内存或 checkpoint 状态到 B；
- 不把“checkpoint 能 restore”当作“位置语义相同”；
- 不保证任意两个功能路径不同的 binary 都能对齐；
- 第一版限定为同源、同输入、单线程、确定性 workload。多线程以后通过 task/barrier 逻辑时钟扩展，不把单线程 occurrence 直接套用到全局线程交错。

## 2. 现有实验可以证明什么

早期实验曾使用 `cross_elf_checkpoint.py` 的 `prepare -> align -> checkpoint -> report` 骨架；该实现现已归档删除。当前流程改由 `src/checkpoint_align/cli.py` 驱动动态 occurrence 事件，旧 BBV 数值只作为历史结果。

当前六 workload 数据如下：

| 项目 | 当前结果 | 正确解读 |
|---|---:|---|
| A SimPoint 总数 | 209 | 可作为无标签回放集和性能基线 |
| BBV 门槛接受 | 50 | 是经验门槛通过数，不是正确映射数 |
| 未通过当前混合门槛 | 159 | 现有状态字段混合了低相似度和低 margin，不能全部解释为重复 phase 歧义 |
| 接受且 `A index != B index` | 8 | 真正发生位置偏移的候选尚未运行闭环 |
| 已执行 checkpoint pair | 4 | 全部恰好是 `A index == B index` |
| 四个 pair 各自最低 restore 后 BBV overlap | 0.799/0.927/0.946/0.945 | 只证明 bounded proxy window 相似，数值为四舍五入 |

四组已执行 pair 均成功生成 B-native checkpoint，并且 A/B bounded restore 都是 zero exit 和 NEMU good state。但它们都没有 checkpoint 后的 `HIT GOOD TRAP` 或 workload terminal completion，且结果明确是 `production_eligible=false`。

现有证据支持继续复用的部分：

- provenance manifest 和自包含实验目录；
- 比例仅作为候选搜索提示；
- BBV interval 与邻域上下文评分；
- score、margin、单调性和拒绝门；
- B-native checkpoint 与双边 bounded restore；
- 结构化 JSON report。

不能直接升级为正式结论的部分：

- BBV count multiset 丢弃 BB 身份，无法证明语义对应；
- 当前逐点 greedy 再检查单调性，不是全局最优序列对齐；
- 固定 `+/-8` 隐含局部漂移小于 160M 指令，且不能表达 interval 插入、删除和局部伸缩；
- 定位和 restore 验证都使用相近的 BBV proxy，证据不独立；
- `0.55/0.10/0.5` 没有语义真值校准，不能称为通用阈值；
- 50 个 accepted 中 42 个仍是同 index，四个运行样本也都是同 index；
- 旧 `lbm A20` 实验与通用重跑的第二窗口 overlap 分别约为 0.015 和 0.985，B trigger 和 checkpoint SHA 也不同。重复性和触发语义在阈值冻结前必须解释。

一个有利条件是：当前六 workload 的 12 个 A/B ELF 都同时包含 `.debug_info` 和 `.symtab`，可以直接启动 DWARF/source-anchor MVP。

当前脚本仍会在 `prepare` 中复制 `weights0`，并为旧 NEMU cluster 输入生成内容为 `1.0` 的兼容文件。这些文件只满足现有 runner 的格式要求，alignment 算法不读取它们，也不能进入位置 schema、评分或报告语义。M1 将其标成内部 `runner_compat` 产物；marker trigger 可用后再移除该依赖。

## 3. 推荐模块设计

### 3.1 三种 interface 的比较

| 设计 | Interface | 优点 | 问题 | 结论 |
|---|---|---|---|---|
| 一键式 | `align_positions(request)` | 常见调用最简单 | 昂贵 trace 的缓存和跨配置复用不显式 | 可作为 CLI 便捷封装 |
| 深模块式 | `PositionAligner().align(...)` | 同一运行证据可复用；manifest 绑定清楚；适合 A->B/C/D | 需要显式提供动态事件 | 当前公开 interface |
| 研究流水线 | `collect` + `match` + `validate` | 易做 evidence 消融和算法实验 | 调用者必须理解 evidence 生命周期和内部策略，interface 偏浅 | 只作内部/专家 interface |

推荐公开一个深模块 `PositionAligner`：

```python
result = PositionAligner().align(
    {"run": build_a, "events": events_a},
    {"run": build_b, "events": events_b},
    correspondence={},
    source_position=source_position,
)
```

这个 interface 只要求调用者理解 build/run、源位置和结果；`policy_version` 是用于复现的已冻结策略标识，不要求调用者自行组装阈值。候选召回、证据融合、全局优化、门槛和拒绝逻辑全部藏在 implementation 中。新增 IR、DWARF 或 DCFG 证据时，变化集中在模块内部，不扩散到调用者。

### 3.2 必须满足的 interface invariants

- A/B 属于同一 workload、输入、参数、功能路径和 `icount_domain`；不满足时批次返回 `INCOMPATIBLE_RUN`，不伪造位置级结果；
- source positions 唯一并按动态顺序严格递增；用于消歧的 context window 可以重叠，但不能改变待映射位置；
- accepted 的所有 B 位置唯一、严格单调、无 crossing；
- 一对多无法消歧时返回 gap，不静默选择局部最高分；
- 比例和 BBV-only 结果最高只能是实验级；
- 每个 B1 绑定 target Build-ID、输入 hash 和运行 manifest hash；
- 输出不包含权重，也不承诺代表性；
- B checkpoint 如需生成，必须由独立 materializer 从 B 自身启动生成。

### 3.3 内部 seam 和 adapter

只有确实存在多种实现的位置才建立 adapter：

| Seam | Adapter | 用途 |
|---|---|---|
| Evidence | `IRMarkerAdapter` | 共同 canonical IR 时的高置信坐标 |
| Evidence | `DwarfSourceAdapter` | 当前带 DWARF ELF 的主路线 |
| Evidence | `DCFGAdapter` | 内联、展开或源码信息不足时的结构匹配 |
| Evidence | `BBVAdapter` | 当前已有的粗召回和低成本辅助证据 |
| Runner | `NemuRunnerAdapter` | B-native checkpoint 和运行验证 |
| Runner | `QemuRunnerAdapter` / native runner | 跨模拟器复核或快速 trace |

profile 目录读取目前只有一种格式，先作为内部 implementation，不额外制造假 seam。纯评分、动态规划和结果判定属于进程内计算，也不需要 adapter。

checkpoint materializer 是 `AlignmentBatch` 的下游消费者，不放进 `PositionAligner` interface。这样定位成功、物化失败和 restore 失败是三个可区分状态。

## 4. 核心方法

### 4.1 冻结运行身份

每个 build/run manifest 至少记录：

```text
workload/source/input/args/env
ELF SHA-256 和 Build-ID
compiler/version/flags/link flags
rootfs/firmware/kernel/DTB
thread count/affinity/seed
interval 定义和 icount_domain
程序输出 hash 和 terminal marker
```

先验证 A/B 功能输出一致。同一 build 重跑时，语义事件序列必须稳定；若不稳定，结果为批次级 `NONDETERMINISTIC_TRACE`，不进入位置匹配。

### 4.2 建立配置内索引

每个 build 的索引包含：

- `anchor_catalog`：静态锚点身份和该 build 下的相对 PC；
- `event_trace`：动态 `(anchor_id, occurrence, workload_icount, context)`，触发语义由 run manifest 统一定义；
- `coarse_phase_index`：BBV/调用序列等低成本检索特征；
- manifest hash 和 evidence schema version。

锚点优先级：

1. 共同 canonical IR 的稳定 IRBB marker；
2. 明确插入且不改变代码生成的 source/IR marker；
3. DWARF 的函数、inline chain、源码行列/discriminator 和 loop identity；
4. DCFG 的调用图、循环图和拓扑 fingerprint；
5. BBV phase signature，只用于召回和实验级结果。

推荐把 source anchor 的硬身份保持稳定，把易随编译变化的属性留给匹配评分：

```text
hard identity:
  canonical source unit + stable lexical entity + event kind

matching attributes:
  demangled signature + inline chain + lexical loop path
  + source line/column/discriminator + call/CFG context
```

`canonical source unit` 使用仓库相对路径，或源码内容 hash 加显式 path remap；不能包含 checkout 的绝对构建路径。path remap 必须写入 manifest。若优化后两个 build 的硬身份不同，由 DCFG/IR adapter 建立 anchor correspondence，而不是假装 ID 相同。

### 4.3 候选召回

总指令比例和 BBV 只生成候选带，不决定结果：

```text
ratio/progress estimate -> coarse band
local BBV/context       -> top-K phase candidates
semantic anchors        -> occurrence candidates
```

搜索窗不能固定为 `+/-8`：

- 初始窗口可以由比例和历史 drift 给出；
- 若 best candidate 落在窗口边缘，必须自动扩窗；
- 扩窗后 best 变化，继续扩展或拒绝；
- 到达资源上限仍未稳定，返回 `SEARCH_TRUNCATED`，不能接受边缘点。

每个候选保留独立证据分量，不只保存一个混合 score：

```text
anchor_identity
sequence_context
local_bbv
progress_consistency
call/loop_context
```

### 4.4 全局序列对齐

先对齐完整或稀疏语义事件序列，再查询 A 的选点；不能让映射结果依赖本次恰好选择了哪些 SimPoint。长 workload 不保存或一次性优化全部事件：优先只保留候选函数/循环的稀疏 marker，按 phase/调用栈分段运行 DP，并设定 trace 字节数、事件数、内存和时间预算；预算耗尽而路径尚未稳定时返回 `SEARCH_TRUNCATED`。

构造有向候选图：

```text
node: (A_event_i, B_event_j, evidence)
edge: 两个候选保持 A/B 时间顺序
gap: A 中事件在 B 无可靠对应，或反之
```

用带 gap penalty 的动态规划/Viterbi/最短路求全局最优单调路径：

```text
maximize:
  semantic evidence
  + sequence context
  + local phase compatibility
  - gap penalty
  - abnormal warp penalty

subject to:
  B event order strictly increases
  no crossing
  accepted boundary is unique
```

同时保留全局第二优路径。最终 margin 应比较 top-1/top-2 全局路径，而不是只比较某个 A point 的局部第一、第二候选。循环展开或向量宽度变化可以通过 occurrence transform 和 gap 表达，不要求 `occurrence_A == occurrence_B`。实现上可用分段 DP、beam width 或 checkpointed backtrace 控制内存，但不能放松严格单调和 gap 约束。

### 4.5 将 A1 投影到 B1

对 A 的待映射位置：

1. 查找恰好命中的或左右括住 A1 的语义事件；
2. 通过全局事件映射找到对应 B 事件；
3. 若 A1 恰好命中事件，输出 B 的对应 marker occurrence，标记为 `exact`；
4. 若只能吸附到相邻公共锚点，输出 A 侧吸附距离及 B1，标记为 `snapped`；
5. `snapped` 只有在位移不超过版本化 policy 中由真值集校准的 `max_snap_distance`，且前后上下文唯一时才可正式接受；
6. 若只能在两个锚点间按比例插值，标记为 `interpolated`，只作实验结果；
7. 候选歧义、crossing、超出 trace 或上下文立即发散时返回 rejected。

结果应把三个维度分开：

| 字段 | 取值 | 含义 |
|---|---|---|
| `anchor_confidence` | H/M/L/R | A/B 语义锚点是否可靠对应 |
| `position_fidelity` | exact/snapped/interpolated/rejected | B1 对原 A1 位置的保真程度 |
| `validation_state` | candidate/validated/failed | 是否完成独立运行验证 |

不要把三者压成一个 `confidence`。

### 4.6 置信等级

| 等级 | 证据 | 允许用途 |
|---|---|---|
| H | 相同 canonical IR/source marker，动态 occurrence 和上下文精确对应 | 正式位置候选 |
| M | DWARF/source 或高质量 DCFG 对应，且全局动态序列唯一 | 正式位置候选，保留审计证据 |
| L | BBV proxy、snap 或 interpolation | 实验、人工复核，不自动发布 |
| R | 比例-only、歧义、crossing、证据不足或验证失败 | 拒绝 |

正式默认只接受 `anchor_confidence in {H,M}`，且 `position_fidelity=exact` 或满足已校准位移上限的 `snapped`。`interpolated` 和 BBV-only 结果保持实验级。位移上限必须写在版本化 policy 中，不能隐藏在 scorer 中。

## 5. 建议输出格式

下面只定义位置对齐需要的最小信息：

```json
{
  "schema_version": 1,
  "workload_id": "...",
  "source_build": "A_BUILD_ID",
  "target_build": "B_BUILD_ID",
  "source": {
    "interval": 20,
    "requested_icount": 400000000,
    "context_end_icount": 420000000,
    "aligned_event": {
      "anchor_id": "loop:A:...",
      "occurrence": 9179,
      "workload_icount": 400001024,
      "snap_delta_instructions": 1024
    }
  },
  "target": {
    "position": {
      "anchor_id": "loop:B:...",
      "occurrence": 9181,
      "image_relative_pc": "0x1234",
      "workload_icount": 401234567,
      "interval": 20,
      "offset_in_interval": 1234567
    }
  },
  "status": "matched",
  "anchor_confidence": "M",
  "position_fidelity": "snapped",
  "validation_state": "candidate",
  "global_path_margin": null,
  "evidence": {},
  "manifest_hashes": {}
}
```

示例数字只说明 schema。`global_path_margin` 的接受阈值必须由标注数据校准；示例中的 `null` 表示尚未计算。格式中没有 weight 字段。

位置级拒绝原因至少包括：

```text
NO_ANCHOR
NO_CANDIDATE
SEARCH_TRUNCATED
AMBIGUOUS
CROSSING
MARKER_GAP_TOO_LARGE
LOW_FIDELITY
OUT_OF_TRACE
POST_ALIGN_DIVERGENCE
```

批次级错误包括：

```text
INCOMPATIBLE_RUN
NONDETERMINISTIC_TRACE
ARTIFACT_MISMATCH
EVIDENCE_COLLECTION_FAILED
```

`NO_CANDIDATE` 表示当前证据找不到，不等于语义上不存在对应位置。

## 6. 验证设计

### 6.1 先建立 ground truth

当前 209 个点没有语义标签，不能用于计算准确率。需要四类可复用校准集：

1. Identity：A->A，所有已标注边界必须 exact，专门发现 off-by-one 和触发时刻错误；固定长度 point 的算术 self-map 另行报告，不把它当作语义真值；
2. Controlled positive：带稳定 marker 的同 workload 多配置构建，人工知道正确 occurrence；
3. Real shifted positive：真实程序中明确 `A index != B index` 的映射；
4. Position negative：在相同输入下构造目标事件缺失、重复 phase 无法唯一消歧、局部事件插入/删除或已知错误的 correspondence，算法必须拒绝；
5. Protocol negative：改变输入、参数或功能路径，必须在 manifest compatibility 阶段返回 `INCOMPATIBLE_RUN`，不计入位置 precision。`occurrence +/-1` 通常是相邻合法事件，只作为 boundary perturbation 测试并按真实标签评估，不能预设为负例。

训练/校准和验证应按 workload/config pair 划分，不能把同一 trace 的不同 point 随机拆到两边，否则阈值会泄漏 workload 特征。

`gold-correspondence.json` 至少记录两侧 build ID、anchor ID、local occurrence、event phase、expected status 和标注来源。`exact` 要求 anchor/occurrence/event phase 完全一致，不给 occurrence 设置容差；两侧 icount 是各自运行的观测值，不要求数值相等。`snapped` 单独记录请求位置到 A anchor 的指令位移，并按冻结 policy 判定。

### 6.2 核心指标

| 指标 | 作用 |
|---|---|
| accepted precision | 已接受映射中有多少符合语义真值；首要指标 |
| coverage | 可映射位置中自动接受多少；次要指标 |
| typed reject distribution | 判断瓶颈是锚点、搜索还是歧义 |
| exact/snapped/interpolated 比例 | 衡量原切片边界保真度 |
| boundary error | B marker occurrence、icount 或 interval offset 的误差 |
| crossing count | 全局顺序错误，正式结果必须为 0 |
| rerun stability | 同 build 重跑映射是否一致 |
| profiling cost | trace 大小、运行 slowdown、索引和匹配时间 |

不以接受率为优化目标。错误接受会把错误位置固化成 checkpoint，因此优先降低 false accept；证据不足时拒绝是正确结果。

首次把某个 capability tier 标为“可复用”前，建议冻结如下 evidence floor；它不是 scorer 阈值：

- calibration 与 held-out 按 workload/config pair 完全隔离；
- held-out 覆盖至少 5 个 workload、3 个 build pair 和 2 类配置差异；
- 每个发布的 tier 单独报告 `correct accepted / labeled accepted`、exact binomial 95% confidence interval 和 coverage；
- H/M accepted precision 的单侧 95% 置信下界至少为 95%。若观察到零次错配，至少需要 59 个 held-out accepted 才能达到这一证据量级；
- 其中至少 10 个是真实非零位置偏移，覆盖至少 3 个 workload 和 2 类配置差异；
- 至少 20 个同输入的 ambiguity/missing-event negative 全部不被接受，并报告拒绝原因；
- coverage 不设脱离用途的统一下限，但必须按 workload、配置差异和置信等级完整报告。

样本不足的 tier 继续标为 experimental。上述样本门槛可以在实验开始前根据成本修订，但一旦查看 held-out 结果就不能再追着结果修改。

### 6.3 PositionAligner 独立验证

PositionAligner 的验收不依赖 checkpoint。对通过 H/M 门槛的位置：

1. 在 held-out trace 中核对 B1 是否命中 gold `(anchor_id_B, occurrence_B)`；
2. 核对 B1 前后的独立 source/IR marker 序列，不使用同一 BBV score 自证；
3. 从头运行 B，确认目标事件唯一可达，运行输出与 manifest 一致；
4. exact 的 occurrence 必须完全一致，A/B run 都必须满足固定的 `before_instruction` 契约；snapped 的 A 侧位移必须不超过冻结 policy 的上限；
5. 对不确定、路径 crossing 或上下文发散的结果检查 typed reject，而不是只调低分数。

### 6.4 可选 checkpoint materializer 验证

只有确实需要 B-native checkpoint 时才执行：

1. B 从头运行，在目标 marker occurrence 生成 B-native checkpoint；
2. restore 后确认 capture marker 不会重复消费；
3. 明确命中 ROI start 和 ROI end；
4. 与 B from-scratch 对照 marker 序列和输出；
5. 在 workload manifest 指定的 terminal marker 上完成终态检查，不能假设所有 workload 都使用固定字面量；
6. BBV overlap 只作为辅助诊断，不能单独升级语义置信等级。

若 checkpoint materialization 因机器契约失败，应记录为 `MATERIALIZATION_FAILURE`，不能反向判定 mapping 错误。若 marker/ROI/终态不一致，则是 `POST_ALIGN_DIVERGENCE`，应否定该映射。当前四组 bounded restore 不具备 marker/terminal 验证，因此只保留为旧 pipeline 的实验级证据。

## 7. 泛化实验矩阵

泛化不等于所有输入都返回 matched，而是不同证据条件下能稳定返回“正确位置或正确拒绝”。建议按难度逐层推进：

| 层级 | A/B 差异 | 目的 | 预期能力 |
|---|---|---|---|
| E0 | 同 ELF 重跑 | identity、确定性和边界语义 | H/exact |
| E1 | 同源码、同编译器、不同 ISA/codegen flags | 复用当前 RVA 配置对 | H/M |
| E2 | 同源码、不同优化级别 | 验证内联、展开和 phase drift | M 或明确拒绝 |
| E3 | 相邻编译器版本 | 验证 source/CFG 变化 | M 或明确拒绝 |
| E4 | GCC 对 Clang | 验证 DCFG fallback | M/L/R，不能保证全覆盖 |
| E5 | 不同输入或功能路径 | 协议负对照 | 批次返回 `INCOMPATIBLE_RUN` |
| E6 | stripped ELF | 评估 DCFG-only 下限 | 后续 capability tier |

E1-E4 的组合评估矩阵（以及 M3 的真实位移验证）应包含：

- 计算密集、内存密集和明显循环结构的 workload；
- 短程可完整运行的校准 workload；
- 重复 phase 容易误配的 workload；
- 在 E1-E4 中分布真实非零 interval 偏移样本。E0 是 identity，E5 是协议 negative，E6 是否具备偏移样本取决于是否有可用的带符号对照。

当前六 workload 可继续作为第一批回放集。现有 8 个 shifted accepted 中，有 4 个恰好位于固定搜索窗边缘，必须先扩窗并确认 best 稳定；不能直接把它们当成功样本。

这里“搜索窗边缘”严格定义为 `abs(target_point - ratio_search_center) == radius`。当前四个是 `cactusADM 17701->17693`、`31012->31004`、`45341->45333` 和 `libquantum 44820->44828`，默认 `radius=8`。

## 8. 分阶段实施路线

以下是粗略单人工程量，长时间仿真和队列等待另计。

v1 只实施 M0-M3：当前六个 workload、E0/E1 以及一组 E2、DWARF/source + BBV、一个本地 collector 和 NEMU 运行验证。DCFG、IR fast path、QEMU、多线程以及大规模 workload 批处理都是后续 capability tier，不能为了“通用”在 v1 同时铺开。

### M0：协议、重复性和真值基线（3-5 个工程日）

任务：

- 冻结 `BuildRun`、`SourcePosition`、`PositionCorrespondence` 和 `AlignmentResult` schema；
- 明确 workload icount 域、零基 interval 和 event phase；
- 实现 A->A self-map 和同 build 重跑对比；
- 构造一个短程、带稳定 marker 的 controlled workload，并生成 `gold-correspondence.json`：记录 A/B build、两侧 anchor/occurrence/event phase、expected status 和 snapped 位移真值；
- 调查两次 `lbm A20` 第二窗口严重不一致的原因，保存两次完整命令、运行日志、checkpoint SHA、profile SHA 和触发 icount；
- 给输入变化、删除锚点、明确错 workload/功能路径和重复 phase 建立负例；occurrence +/-1 作为带标签的边界扰动，不预设拒绝。

完成标准：

- A->A 所有 identity golden 边界 100% exact；固定长度 interval self-map 另报 100% 算术一致；
- 同 build 重跑事件序列与映射稳定；
- trigger 的 before/after 语义无 off-by-one；
- `lbm` 至少完成 3 次同参数复跑并给出日志/哈希对照；差异被解释，或明确隔离为不能用于校准的非确定性样本；
- 正负例能被结构化记录，后续阈值有真值可依赖。

### M1：建立通用 PositionAligner 骨架（5-8 个工程日）

任务：

- 将当前脚本中的“位置定位”和“checkpoint 物化”拆开；
- 实现 `PositionAligner().align(...)` 动态 interface；
- 运行时 occurrence 事件作为唯一位置证据；
- 在当前查询的 BBV points 上，用带 gap 的 sparse monotonic DP 替换逐点 greedy；该结果仍依赖查询集合，只是 M2 全事件对齐的实验级基线；
- 加 top-2 sparse path 和 typed rejection；
- 搜索窗口自适应扩展，边缘命中不得直接接受；
- 新增 `SEARCH_TRUNCATED` 状态，并记录事件数、trace 字节数、内存/时间预算及触发原因；
- 把特征归一化、gap/warp penalty、tie rule、预算和 snap 上限写入版本化 policy；M2 真值校准前不发布默认门槛；
- 输出独立证据分量和 manifest hash；旧 NEMU 所需 dummy weight 标为 `runner_compat`，不进入 alignment 结果。

完成标准：

- 对 209 点可重复回放；
- accepted 结果无 crossing、无搜索截断和静默 fallback；
- 每个 rejected 都有稳定原因；
- 本阶段不要求 accepted 数增加，也不把 BBV 结果升级到 H/M；它不声称已经实现与查询点集合无关的全事件对齐。

### M2：DWARF/source-anchor MVP（8-15 个工程日）

任务：

- 离线建立 PC/basic block 到函数、inline chain、源码位置和 loop 的映射；
- 先实现离线/native collector 采集 function-entry 和可可靠恢复的 loop marker 稀疏事件流；M2 不要求 NEMU/QEMU marker trigger，模拟器集成放在可选 M5；
- 实现 `DwarfSourceAdapter` 和与 source-position 查询集合无关的 occurrence sequence alignment；
- BBV 降为 coarse retrieval 和辅助 evidence；
- 选 3-5 个确定性 workload 构建语义 golden set。

完成标准：

- 当前 12 个带 DWARF ELF 可生成 anchor catalog；
- identity 和 controlled positive 的 gold marker occurrence/event phase 100% exact；
- real workload 的 H/M accepted 均可人工追溯到函数/循环 occurrence；
- 在 held-out workload/config pair 上报告 accepted precision 和 coverage，而不是只报 score。

### M3：真实位移和跨配置验证（8-15 个工程日，加仿真时间）

任务：

- 重新检查当前 8 个 `A index != B index` 候选，先处理搜索窗边缘问题；
- 至少闭环一个真实 shifted pair；
- 增加 E2/E3 配置对，覆盖优化级别和编译器版本变化；
- 对重复 phase、loop unroll 和局部 phase insertion 做消融；
- 比较 ratio-only、当前 BBV greedy、全局 BBV、DWARF+global 四种方法。

完成标准：

- 首个 shifted pair 用于尽快跑通闭环；阶段完成时至少 3 个真实 `A_i -> B_j, i != j` 在至少 2 个 workload 上达到 H/M exact 或 policy 内 bounded-snapped；
- B1 由独立语义 marker 验证，不是由同一种 BBV proxy 自证；
- calibration/held-out 分离，逐项报告正确数、错误数、coverage 和拒绝类型；M3 是扩展验证阶段，最终通用性仍以第 6.2 节的 evidence floor 为准；
- 新配置对不依赖手工修改算法常量。

### M4：DCFG/IR 能力扩展（按 M3 结果决定）

任务：

- source anchor 因内联/展开不足时加入 call graph、loop graph 和 DCFG affinity；
- 对共同 canonical LLVM IR 的构建加入 IRBB fast path；
- 对 stripped ELF 只评估 capability 下限，不在 v1 中自动宣称正式匹配；
- 阈值按 evidence capability tier 校准，不共享一个 magic number。

完成标准：

- E2/E3 的 coverage 相对 M2 提高，且 held-out accepted precision 及其置信下界不退化；
- graph match 的每个 M 级结果都有动态序列唯一性证据；
- E4/E6 无足够证据时能稳定拒绝。

### M5：B-native 物化和批量化（位置方法稳定后）

任务：

- RunnerAdapter 支持 `(anchor_id, occurrence)` 触发，并在 run 级固定为 `before_instruction`；
- checkpoint/sidecar 持久化 anchor counters、global event sequence 和 marker-consumed state；
- 验证 ROI start/end 和最终 terminal marker；
- 扩展到 profile roots 中经 manifest、输入和 terminal marker 检查确认可用的 workload（当前已验证六个，57 只是待核对的上限），生成 suite-level precision、coverage、reject taxonomy 和成本报告；
- 冻结版本化 policy，仅对已校准 capability tier 发布默认门槛。

完成标准：

- 每个正式 B checkpoint 都绑定 B build/run manifest；
- restore 后 marker 状态无重复消费和 off-by-one；
- 正式样本均命中 ROI 协议并正常终止；
- 未通过位置门槛的样本不会启动 checkpoint 生成。

## 9. 最近一轮工作的明确顺序

建议下一轮严格按这个顺序，不先扩跑全部 workload：

1. 复现并解释 `lbm A20` 两次结果差异；
2. 冻结 Position/trigger/icount schema；
3. 做 A->A self-map 和 controlled positive/negative；
4. 将当前 BBV locator 改为全局序列匹配和自适应搜索；
5. 用现有 209 点回放，但只把它们当无标签基线；
6. 利用现有 DWARF 做 function/loop occurrence trace；
7. 再验证一个真实 shifted pair；
8. 通过后才增加更多优化级别、编译器版本和经 manifest 核对可用的 workload 批量实验。

第一阶段最有价值的交付不是“更多 accepted”，而是下面三个事实首次同时成立：

```text
知道 A1/B1 的语义真值
算法能找到 B1 或正确拒绝
独立 runtime evidence 能复核 B1
```

## 10. 最终验收条件

### 10.1 PositionAligner

位置对齐模块达到可复用状态，应同时满足：

- interface 中不含权重，调用者只提交运行身份和 A 位置；
- identity golden self-map 精确且重跑稳定；
- 所有 accepted 结果全局单调、唯一、无 crossing；
- 比例-only、搜索截断和 interpolation 不会进入正式结果；
- 特征、penalty 和阈值来自按 workload/config pair 隔离的 calibration set，并由版本化 policy 固定；
- 每个宣称支持的 capability tier 分别满足第 6.2 节的 held-out 样本量、precision 置信下界、shifted case 和 negative case 门槛；
- H/M 结果有独立语义事件证据；
- exact/snapped 的判断和位移可从 gold correspondence 复核；
- 对无法支持的配置明确返回原因，而不是伪造 B1。

PositionAligner 不要求 checkpoint。满足这些条件后，相应 capability tier 才可以被描述为“跨配置位置对齐”；在此之前，当前实现应继续称为“BBV-based experimental candidate locator”。

### 10.2 可选 checkpoint materializer

只有需要把 B1 物化成 checkpoint 时，才追加以下验收：

- B checkpoint 从 B 自身启动生成，并绑定 B build/run manifest；
- trigger 精确命中 `(anchor_id_B, occurrence_B)`，且 run 级语义为 `before_instruction`；
- restore 后 marker state 不重复消费且无 off-by-one；
- 若消费方定义了 ROI，则命中 ROI start/end；
- 按 workload manifest 的终态协议正常完成。

materializer 失败和位置匹配失败必须分别报告；不需要 checkpoint 的调用者不受这组条件约束。
