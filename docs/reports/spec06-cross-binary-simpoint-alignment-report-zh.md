# SPEC CPU2006 跨 ELF SimPoint 选点对齐实施报告

## 1. 报告目的

本文给出一个面向实际 profile 数据的方案：将

```text
/nfs/home/share/checkpoints_profiles/spec06_gcc16_rv64gcb_260724
```

中的 SimPoint 选点位置，对齐到

```text
/nfs/home/share/checkpoints_profiles/spec06_gcc16_rva23_novec_260726
```

本文只讨论“选点位置/语义区域”的对齐，不讨论 SimPoint 权重迁移，也不把 A 的 checkpoint 状态直接迁移到 B。

## 2. 结论摘要

方案可行，但不能直接复制 `simpoints0` 中的 point 数字、PC 或原始动态指令数。

两套 ELF 的机器代码不同，同一个 `point` 只表示各自程序执行过程中的第几个 20M 动态指令区间。正确做法是：

```text
A 的 SimPoint interval
    -> A 中的源码/函数/循环语义锚点
    -> A/B 动态锚点序列匹配
    -> B 中对应的动态 occurrence
    -> B 的 interval 或精确 marker 位置
    -> 从头运行 B 生成 B-native checkpoint
```

建议先实现 interval 级映射，验证流程后再增加 marker 级精确触发。

## 3. 现有数据和适用边界

### 3.1 已确认的目录结构

两套 profile 均包含：

```text
bin/ cfg/ profiling/ cluster/ checkpoint/ json/ stamps/
```

并且各有 57 个同名 workload 的 JSON、profiling 和 cluster 产物。已有结果的主要入口是：

```text
profiling/<workload>/simpoint_bbv.gz
cluster/<workload>/simpoints0
cluster/<workload>/weights0
json/<workload>.json
checkpoint/<workload>/<point>
```

### 3.2 关键证据

两边的 profiling 日志都显示 interval 为 20,000,000。以 `namd` 为例：

| 项目 | A: rv64gcb_260724 | B: rva23_novec_260726 |
|---|---:|---:|
| 总动态指令数 | 1,722,583,019,975 | 1,725,110,728,365 |
| `simpoints0` 点数 | 64 | 23 |
| ELF | `elf/namd.elf` | `elf/namd.elf` |
| ELF SHA-256 | 不同 | 不同 |
| DWARF/symbol | 有 | 有 |

A 使用 `-march=rv64gc_zba_zbb_zbs_zbc`；B 使用 RVA23 相关扩展集合且无向量选项。两者不能假设 PC、基本块编号、机器指令长度或动态指令位置相同。

### 3.3 必须满足的前提

开始映射前，必须确认 A/B：

1. workload 输入、参数和启动脚本一致；
2. rootfs、固件和运行环境一致到实验要求的范围；
3. 程序输出一致；
4. 单线程运行可重复，或多线程已有 record/replay；
5. SimPoint 的 icount 统计域一致，例如是否只统计目标进程用户态指令。

若功能路径、输出或线程执行顺序不一致，应停止声称“精确对齐”，改为在 B 上独立 profiling。

## 4. 对齐对象的定义

### 4.1 A 中的原始选点

令 A 的 SimPoint 为 `p`，interval 为 `L=20,000,000`。在确认 profiler 为零基且边界语义一致后：

```text
A_start = p * L
A_end   = (p + 1) * L
```

该公式必须先通过 A->A self-map 验证，不能盲目假定。

### 4.2 跨 ELF 的语义锚点

建议使用如下锚点身份：

```text
规范化源码路径
+ demangled 函数签名
+ inline 调用链
+ 源码行/列/discriminator
+ 循环嵌套层次
+ 事件类型
```

事件类型至少包括：

```text
function-entry
loop-header
loop-backedge
```

一个动态事件应表示为：

```text
(anchor_id, occurrence, image-relative-pc, icount, context)
```

不能只使用绝对 PC，也不能只使用源码行号。宏展开、内联和同一行多个语句都可能造成歧义。

## 5. 推荐实施方案

### 阶段 0：冻结实验 manifest

为每个 workload 保存：

```text
workload/input/args/env
ELF SHA-256 和 Build-ID
compiler/version/完整 flags/link flags
rootfs/firmware/kernel/DTB 标识
线程数、绑核、随机种子
interval 和 icount_domain
```

manifest 必须绑定 A、B 两个 ELF，避免后续将生成的 B checkpoint 误配到其他构建产物。

### 阶段 1：采集 A/B 轻量动态锚点轨迹

在现有 NEMU/QEMU profiling 旁增加一个 trace 输出，建议按 basic-block 或函数/循环事件采集，不记录每条指令。

每条记录至少包含：

```text
build-id
image-relative-pc
anchor_id
event_kind
occurrence
workload_icount
```

两边 ELF 已保留 DWARF 和符号表，因此第一版不必改动编译器，只需将运行时 PC 映射回 DWARF/source anchor。

如果现有插件无法提供 inline/source 信息，应先离线建立：

```text
PC/basic-block -> function -> DWARF source location -> anchor_id
```

### 阶段 2：建立静态锚点映射

优先级如下：

1. 相同函数签名、源码位置和 inline 调用链；
2. 相同 loop header/backedge 的源码上下文；
3. 函数调用邻域和循环拓扑；
4. DCFG fingerprint 作为兜底。

输出 `anchor-map.json`，每条映射保留证据和置信等级：

```text
H: 源码/IR 锚点精确匹配
M: 图结构和动态上下文高分匹配
L: 仅能通过吸附或插值确定
R: 无法可靠映射
```

### 阶段 3：动态 occurrence 序列对齐

对每个 A/B 锚点对，使用动态 occurrence 序列进行单调匹配：

```text
A: anchor_a occurrence k_a
             <->
B: anchor_b occurrence k_b
```

匹配必须满足：

- 全局顺序单调；
- 不发生事件交叉；
- 前后上下文相似；
- 起止锚点在 B 中唯一；
- 局部动态指令数比例没有明显异常。

允许 `k_a` 与 `k_b` 不相等，因为不同代码生成可能改变循环展开、调用次数或向量化路径。

### 阶段 4A：interval 级映射（最小改动）

对 A 的每个选点 `p`：

1. 找到 `[A_start,A_end)` 两侧最近的 A 语义锚点；
2. 将两个锚点 occurrence 映射到 B；
3. 得到 B 的动态起止 icount；
4. 用 B 的 interval 计算目标 point：

```text
q = floor(B_start_icount / 20,000,000)
```

5. 生成一份新的 B `simpoints0`，其中 point 使用 `q`；
6. 保留 sidecar 记录真实语义边界及其在 B interval 内的偏移。

这种方法把 B 的语义起点量化到 interval 起点，起点量化误差小于 20M 指令，适合先跑通全流程。但固定 20M ROI 不自动保留映射得到的 B 语义终点，因此 interval 级结果只能称为近似位置对齐。生成的 q 是“B 中对应的 interval”，不是 A point 的数值复制。

### 阶段 4B：marker 级映射（正式方案）

如果需要更精确的语义位置，应扩展 checkpoint trigger，使其支持：

```text
target_anchor_id
target_occurrence
trigger_semantics
```

B 从程序入口运行，在目标 marker occurrence 处触发 checkpoint。建议把 ROI 定义为半开区间：

```text
[roi_start, roi_end)
```

checkpoint 内或 sidecar 中保存：

```text
anchor counters
global event sequence
one-shot stage
capture marker 是否已消费
```

这样 restore 后可以继续匹配 occurrence，避免重复计数或 off-by-one。

## 6. 比例换算的正确定位

可以用总指令数比例做 sanity check，但不能将其作为正式映射：

```text
q_ratio = round(p * B_total / A_total)
```

例如 `namd` 中：

```text
B_total / A_total = 1.001467
A p=71094 -> q_ratio=71198
```

这个数字只能用于发现明显异常。正式结果必须来自语义锚点和动态 occurrence；不能只依据总指令数比例。

## 7. 输出产物

建议为每次对齐建立独立 namespace：

```text
alignment/<workload>/
  manifest.json
  A/anchors.jsonl.zst
  B/anchors.jsonl.zst
  anchor-map.json
  aligned-regions.json
  B-simpoints0
  B-region-sidecar.json
  logs/
```

每个 region 至少包含以下字段。下列数值仅用于说明格式，不是已经计算出的 `namd` 映射结果：

```json
{
  "source_simpoint": 71094,
  "target_interval": 71198,
  "target_start_icount": 1423968123456,
  "target_end_icount": 1423988123456,
  "start_anchor": "...",
  "end_anchor": "...",
  "start_occurrence": 9181,
  "end_occurrence": 407,
  "anchor_confidence": "M",
  "fidelity": "snapped"
}
```

`fidelity` 建议使用：

```text
exact        起止语义边界都精确命中
snapped      吸附到相邻公共锚点
interpolated 插值获得
rejected     无法可靠映射
```

## 8. 验证与验收标准

### 8.1 对齐正确性

1. A->A self-map 的 point、occurrence 和边界完全一致；
2. A->B 映射全局单调且无 crossing；
3. 每个 B 起止锚点唯一命中；
4. B 的 PC 可通过 DWARF 映射到预期函数/循环；
5. 前后上下文的函数/循环序列相似；
6. 每个 region 明确记录 `exact/snapped/interpolated/rejected`。

### 8.2 checkpoint 正确性

1. B checkpoint 能被目标模拟器实际 restore；
2. restore 后能够命中目标 ROI 起止事件；
3. restore 后不会重复消费 capture marker；
4. checkpoint 绑定的是 B ELF、B rootfs 和 B 固件；
5. 从头运行 B 与 restore 运行在目标区域的关键输出一致。

### 8.3 失败处理

以下情况不应静默生成“精确对齐”结果：

- 只能依赖总指令数比例；
- 起止锚点多对多且无法消歧；
- 动态事件发生顺序交叉；
- 输入、输出或线程轨迹不同；
- B 中找不到对应函数/循环区域。

这些 region 应标为 `rejected`，转为 B 独立 profiling 或人工复核。

## 9. 分阶段落地计划

### 第一阶段：5 个 workload 验证

建议选择：

```text
namd
astar_biglakes
bzip2_source
gcc_expr
mcf
```

先实现 interval 级映射，验证 trace、anchor-map、B `simpoints0` 和 checkpoint restore。

### 第二阶段：批量处理 57 个 workload

批处理前先统计：

```text
可映射 region 数
exact/snapped/interpolated/rejected 数量
每个 workload 的序列确定性
checkpoint restore 成功率
```

### 第三阶段：需要正式语义边界时启用 marker trigger

只有当 interval 级方案验证稳定后，才扩展 NEMU/QEMU 的 marker occurrence 触发。这样可以将工程风险限制在少量已验证 workload 上。

## 10. 最终建议

对当前两套 profile，最现实的路线是：

```text
保留 A 的 simpoints0 作为参考选点
不使用 B 的独立 simpoints0 作为对应关系
补采集 A/B 源码级动态锚点
用单调 occurrence 对齐得到 B interval
先生成 B interval 级 checkpoint
再按需要升级为 marker 级 checkpoint
```

权重可以完全排除在第一阶段之外。需要明确的是：即使只关心位置，也必须重新运行 B 到目标位置；不能把 A checkpoint 的寄存器、内存或 PC 状态复制到 B。

本报告对应的原始文献综述和一般化方案见：

[`cross-binary-slice-alignment-survey-and-plan-zh.md`](../design/cross-binary-slice-alignment-survey-and-plan-zh.md)
