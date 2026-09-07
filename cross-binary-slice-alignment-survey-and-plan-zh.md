# 跨编译器版本/配置的 Workload 切片位置对齐

## 文献调研报告与实施方案

日期：2026-08-26

## 0. 结论先行

把配置 A 的切片迁移到配置 B，**不能**做 ELF 地址平移，也不能把 A 的动态指令数位置按总指令数比例换算成 B 的位置。编译器版本、优化级别、ISA 选项、内联、循环展开、向量化和链接库变化都会同时改变 PC、基本块、控制流和动态指令数。

正确的映射对象应是：

```text
语义锚点（函数/循环/IR 基本块）
+ 该锚点在本次动态执行中的第 k 次出现
+ 前后动态上下文
```

最终生成的也不应是“修改过的 A checkpoint”，而应是：

```text
A 中的代表区域
    -> 跨 ELF 的动态语义位置映射
    -> B 中的 warmup/start/end 触发点
    -> 从头运行 B 到触发点
    -> 生成 B 自己的 checkpoint
```

A/B 的栈布局、寄存器 PC、代码地址、堆对象、动态链接状态都可能不同，因此 A checkpoint 的内存和寄存器状态不能直接移植给 B。

本报告的推荐方案是分层处理：

1. 如果 A/B 可以从同一份固定的 canonical LLVM IR 生成，优先采用 Nugget 式 IRBB marker。这条路线最简单、边界最稳定，但不能覆盖任意 GCC/Clang 或会改变 IR 的配置差异。
2. 对任意同源、同输入、可重新运行的 ELF，采用“DCFG 分层图匹配 + 动态 marker 序列对齐”。这是已有 A 切片映射到 B 的主路线。
3. 如果目标是严谨比较 A/B 性能，长期不应先在 A 独立选点再搬运；应先对齐 A/B 的公共区间，构造联合特征，再统一运行 SimPoint。
4. 如果只有两个 ELF 和 `simpoints0`，没有 A/B 的完整动态运行信息，就无法可靠恢复语义位置，必须重新运行 A/B 做一次轻量 profiling。
5. 如果输入、程序输出、线程行为或动态控制流不一致，应停止声称“精确对齐”，改为在 B 上重新 profiling/cluster。

## 1. 问题定义

### 1.1 输入

典型输入为：

- 同一 workload 的配置 A ELF 和配置 B ELF；
- 相同源码版本、输入数据、参数、运行库与 workload 启动协议；
- A 的 interval size、`simpoints0`、`weights0` 和最好仍保留的 BBV；
- A/B 可执行一次完整的功能运行或快速仿真；
- 最好带有 DWARF、符号表或编译器生成的稳定 marker 元数据。

本地既有 SimPoint 流程中的 `simpoints0` 通常是 `<point> <cluster-id>`，`weights0` 是 `<weight> <cluster-id>`。`point` 只表示 A 的第几个动态指令区间，它不是源代码位置，也不是 B 的位置。

### 1.2 需要产出的三个不同结果

应把下面三个概念分开：

| 结果 | 含义 | 是否可直接从 A 复用到 B |
|---|---|---|
| 区域身份 | 这是程序哪一段语义工作 | 可通过跨二进制对齐复用 |
| 动态边界 | B 在哪一次执行到该段工作 | 必须在 B 上重新定位 |
| 代表性权重 | 该区域代表 B 全程序的多少执行 | 必须按 B 重新计算 |

如果只是要“在 B 的相同语义位置生成 checkpoint”，前两项足够；如果还要用这些切片估计 B 的全程序 CPI、cycle 或 A/B speedup，则第三项也必须完成。

### 1.3 形式化描述

设 A 的完整动态执行轨迹为 `T_A`，已有切片为：

```text
R_A = [I_A_start, I_A_end)
```

其中 `I_A_start`/`I_A_end` 是 A 的动态指令位置。对一个静态语义锚点 `a`，定义其第 `k` 次动态事件：

```text
E_A(a, k) = (anchor_id, occurrence=k, pc_A, icount_A, context_A)
```

跨二进制映射要先得到静态锚点对应 `M(a)=b`，再在两条动态事件序列中找到单调、不交叉的事件对应：

```text
E_A(a, k_A) <-> E_B(b, k_B)
```

只有当 A 切片的起止边界都能由这样的事件或高质量的左右括号约束时，才能构造：

```text
R_B = [I_B_start, I_B_end)
```

这里 `k_A` 与 `k_B` 在增强方法中不必相同。例如不同向量宽度会让同一个源循环在 B 中少执行或多执行若干次。需要区分：2007 方法要求公共 marker 总计数一致，因此 exact mapping 使用相同 occurrence；2015 图匹配及本方案的比例序列对齐才允许 `k_A != k_B`。

## 2. 文献调研

目录中的三篇论文恰好构成了该问题的三代方法。

| 文献 | 公共坐标系 | 主要贡献 | 对现有任意 ELF 的适用性 | 核心限制 |
|---|---|---|---|---|
| Cross Binary Simulation Points, 2007 | 函数入口/循环分支及动态出现次数 | 首次系统化选择跨 binary 的同语义 VLI，并重算各 binary 权重 | 中等，需要符号、源码行和完整运行计数 | 严格同名/同计数，复杂优化后 marker 稀疏 |
| Graph-Matching-Based Simulation-Region Selection, 2015 | DCFG 中匹配的函数/循环 + 多轨迹序列对齐 | 用图拓扑、动态计数和时序处理内联、展开、向量宽度等差异 | 最高，可在符号不完整时工作 | 启发式、近似等价、完整 trace 成本较高 |
| Nugget: Portable Program Snippets, 2026 | LLVM IR 指令数、IRBB ID 与出现次数 | 让 interval/marker 脱离最终 ISA，并可在真机快速验证 | 低到中，要求从共同 base IR 重新构建 | 不能处理任意不同 IR、任意既有 ELF 或不稳定控制流 |

### 2.1 Cross Binary Simulation Points（2007）

Perelman 等人指出：分别对 A/B 独立运行 SimPoint，虽然各自的全程序估计可能准确，但两套采样会覆盖不同语义行为，导致比较 speedup 时采样偏差不一致。[1, PDF p.1-3]

论文方法的关键步骤是：[1, Sec. 3, PDF p.4-5]

1. 对每个 binary 的相同输入完整运行一次，统计函数入口、循环入口和循环回边的执行次数。
2. 结合函数名、源码行和动态计数，找到所有 binary 中都存在的 mappable marker。
3. primary binary 每经过约一个目标指令窗口后，在遇到的下一个公共 marker 截断，生成 variable-length interval（VLI）。
4. 只在 primary binary 的 VLI BBV 上运行 SimPoint。
5. 用起止 `(marker ID, execution count)` 把选中区域投影到其他 binary。
6. 按每个 binary 各 phase 的动态指令数占比重新计算权重。

为什么必须用 VLI：同一段语义工作在 A 可能从动态指令 `X` 开始，在 B 从 `Y` 开始，执行长度也可能不同；因此 fixed-length interval 的起止动态指令数不具有跨 ELF 意义。[1, Sec. 3.1, PDF p.4]

该方法能处理少量简单内联，但要求可映射 marker 在各 binary 的总执行次数相同。复杂内联、循环拆分、代码移动或多个同计数循环会造成歧义。论文的 `applu` 案例中，优化器把多个函数内联并拆分循环，剩余结构不足以映射，最终产生了过长区间。[1, Sec. 3.3 and 5.1, PDF p.5-7]

实验覆盖 21 个 SPEC CPU2000 程序的 32/64 位、优化/未优化四种 binary，目标区间为 100M 指令，最多 10 个 cluster。论文结论的重点是 mappable VLI 在跨 binary speedup 比较中保持了更一致的 phase bias；例如独立 FLI 在 `gcc` 的一个比较中造成 38% speedup error，而同语义 VLI 显著缓解了跨 binary phase-bias 不一致。VLI 的单个 phase 仍可能有较大绝对误差。[1, PDF p.6-10]

对本任务的直接价值：它给出了最简单且正确的边界表示，即 `(marker ID, 动态出现次数)`，并明确 B 的权重不可照搬 A。

### 2.2 基于图匹配的多 binary 选区（2015）

Yount 等人针对 2007 方法的严格匹配限制，引入两阶段方法：[2, PDF p.1-2]

1. 对每个 binary 的完整运行记录 Dynamic Control-Flow Graph（DCFG）和保留执行顺序的 edge trace。
2. 把 DCFG 缩减为调用图，以及每个函数内的 loop graph，避免直接匹配数万到数十万个 BB。
3. 对 reference binary 和其余 binary 做分层图匹配。
4. 用匹配函数/循环入口作为 marker，对多条动态 edge trace 做启发式序列对齐。
5. 生成所有 binary 数量相同、语义工作近似相同的 interval，并拼接各 binary 的 BB 特征形成 merged profile。
6. 对 merged profile 统一运行 SimPoint，再为各 binary 分别计算权重和生成 region checkpoint/pinball。

节点 affinity 综合以下信息：[2, Sec. III, PDF p.3-4]

- 符号名的 Levenshtein 距离；
- 动态执行次数差；
- 入度和出度差；
- 源码行号集合差；
- 调用图或 loop graph 的拓扑相似性。

论文使用 GraphM 的 RANK 算法，并以 `alpha=0.5` 平衡拓扑和节点元数据。符号名和源码行是可选项，因此 stripped 或优化严重的 binary 仍有机会依靠结构与动态信息匹配，但置信度会下降。[2, PDF p.4]

动态执行次数不同时，论文按计数比例过滤事件。例如某 loop marker 在 A 出现 4000 次、B 出现 8000 次，则 B 每两次只保留一次作为候选；非整数比例也允许。之后在约 20 倍目标 slice 长度的滑窗内消除顺序交叉，并用局部区间指令数比例是否接近全程序比例来修正匹配分数。[2, Sec. IV, PDF p.5-6]

边界选择依次尝试：[2, PDF p.7]

1. 目标长度 `+/-10%` 内的满分匹配；
2. 同一窗口内的最高分非理想匹配；
3. 在左右 marker 间插值；
4. 向后寻找最多 10 个目标区间，再按比例细分；
5. 最后按剩余指令比例划分。

后三级只是工程回退，不应被报告为“精确语义边界”。尤其是最后的纯比例切分，本方案将其设为拒绝生成正式 checkpoint，而不是静默接受。

论文使用 SPEC CPU2006、Intel `-O3` 的 AVX2/AVX512 两个版本、30M 目标区间和最多 30 个 cluster。29 个 benchmark 中 23 个的 cross-binary speedup error 更低；总体 CPU2006 score error 为 0.20%（独立 SimPoint 为 0.91%），benchmark 平均为 0.69%（独立 SimPoint 为 2.77%）。同时，新方法显著缓解了旧方法中公共 marker 过稀导致的超长区间。[2, PDF p.7-9]

证据边界也必须说明：实验只改变同一 Intel 编译器的 ISA 目标，使用 cache simulator proxy，并未直接验证 GCC 版本切换、GCC 对 Clang、LTO/PGO 或不同 libc。论文把输出称为 approximately semantically equivalent，而不是形式化等价。[2, PDF p.5, p.7-9]

### 2.3 Nugget（2026）

Nugget 把机器指令数替换为执行过的 LLVM IR 指令数，把 IR basic block（IRBB）作为跨最终 binary 的工作单元。[3, Sec. III-A, PDF p.3-4]

其流水线为：

1. 先生成一份优化后的 canonical base LLVM IR。
2. LLVM pass 给每个 IRBB 分配 ID，记录静态 IR 指令数，并在块尾插入 hook。
3. 运行 interval-analysis binary；hook 累加已执行 IR 指令数，生成固定 IR-work interval、IRBB vector 和 count-stamp vector。
4. 选样后，用 `(IRBB ID, occurrence)` 生成 start/end marker。
5. 对每个 ISA/后端配置重新编译。模拟器可直接监控 marker label 在该 ELF 中的 PC；真机可执行 hook。
6. B 仍需从程序入口运行到 marker，或先在 B 上创建 checkpoint 后再恢复。

这一路线的重要前提是各 binary 都来自同一个 base IR。影响控制流的 IR 优化必须先固定完成；论文默认关闭 LTO/PGO，要求纳入分析的静态/链接库也进入 base IR。论文观察到实验中的 IR 在优化级别、CPU feature flag 和 LLVM 版本之间保持稳定，但未给出覆盖任意版本的组合矩阵，因此不能外推到 GCC 对 Clang 或任意前端/中端变化。[3, Sec. III-B, PDF p.4]

Nugget 适用于相同输入下 IR 控制流稳定的程序。论文明确指出浮点精度影响数据依赖循环时不适用；多线程 NPB 即使在同一主机也存在轨迹差异。[3, PDF p.4, p.8]

论文没有提出新的代表区间选择算法，只用 Random 和 IRBB-vector k-means 演示框架。其价值是 portable interval/marker 和快速真机验证，而不是保证任意 sample 都有代表性。实验甚至出现了朴素选样的极端误差；微架构差异对代表性误差的影响也大于单纯 ISA 差异。[3, PDF p.9-12]

性能方面，Nugget 的 interval analysis 相比 functional simulation 平均降低 577.65 倍开销；但单线程、并行 workload 的 slowdown 和 hook 干扰差异很大。[3, PDF p.7-8]

对本任务的直接价值：如果能控制构建链，IRBB marker 是最稳定、工程成本最低的方案；但它不是“任意两个 ELF 的 mapper”，也没有迁移 live-in state。

### 2.4 三篇文献的综合结论

三篇文献共同证明了四点：

1. 跨 ELF 的固定 PC 和固定动态指令窗口都不稳定。
2. 可迁移边界必须由静态语义实体和动态 occurrence 共同定义。
3. 相同语义区域在不同 binary 的长度和权重可以不同。
4. “位置对齐正确”和“样本具有性能代表性”是两项独立验证任务。

它们的差别在于静态语义实体来自哪里：2007 年依赖符号/源码行，2015 年加入 DCFG 拓扑和时序，2026 年通过约束构建链直接固定 IR 坐标。

## 3. 推荐选型

### 3.1 按变更类型选择方法

| A/B 差异 | 推荐方法 | 可声称的对齐强度 |
|---|---|---|
| 相同 canonical LLVM IR，仅后端 ISA、`-mcpu/-march`、代码生成配置不同 | IRBB marker | 高，前提是动态 IR marker 序列一致 |
| 同一编译器版本，`-O1/-O2/-O3` 导致 CFG 有变化 | source/IR marker 优先，DCFG+trace 兜底 | 中到高 |
| GCC 不同版本、GCC 对 Clang、内联/向量化差异明显 | DCFG 分层图匹配 + 动态序列对齐 | 中，必须给置信度 |
| 不同静态/动态库实现 | 把库纳入 DCFG 和映射；否则重新 profiling | 中到低 |
| 只有 stripped ELF | 依靠拓扑、计数、调用上下文匹配 | 低到中，需更强动态验证 |
| 输入、源代码、宏功能、程序输出不同 | 不做精确迁移，在 B 上重选点 | 不可映射 |
| 多线程调度不确定 | record/replay、barrier/task 语义 marker，或分别重选点 | 默认不可声称精确 |

### 3.2 推荐的两种工作模式

#### 兼容模式：从已有 A SimPoint 出发

```text
A 的 simpoints0/interval size
          +
A/B 完整 DCFG 与 marker trace
          |
          v
把 A 固定窗口投影到公共动态锚点
          |
          v
B 的 warmup/start/end marker plan
          |
          v
从头运行 B，生成 B-native checkpoint
```

这条路线以已有 A 结果为候选，但 A 固定窗口通常不会刚好落在公共 marker 上。只有 A 的原起止边界都精确命中公共 marker 时才是 exact reuse；发生边界吸附、VLI 并集替换或插值后，产物就是重新圈定的实验性区域，不能再称为原切片的精确迁移。

#### 推荐模式：联合选区

```text
A/B DCFG + 动态 trace
          |
          v
先生成 A/B 对齐的 variable-length intervals
          |
          v
拼接/融合 A 和 B 的规范化特征
          |
          v
统一运行 SimPoint，得到共同 region identity
          |
          v
为 A/B 分别生成 marker、权重和 checkpoint
```

这更忠实于 2015 论文，也避免“某个 phase 只在 B 中明显，却因只看 A 的 BBV 而完全没有被选中”。

## 4. 总体工程架构

建议把现有 `BBV -> SimPoint -> checkpoint -> metadata` 扩展为：

```text
build manifest
    |
    +--> A collector --> DCFG_A + anchor catalog_A + trace_A
    |
    +--> B collector --> DCFG_B + anchor catalog_B + trace_B
                              |
                              v
                    graph/IR anchor matcher
                              |
                              v
                    monotonic trace aligner
                              |
                  +-----------+-----------+
                  |                       |
                  v                       v
          existing-A projector     unified interval builder
                  |                       |
                  +-----------+-----------+
                              v
                     per-build region plan
                              |
                              v
              B fast-forward + checkpoint generator
                              |
                              v
                  restore/ROI/accuracy validator
```

建议拆成五个模块：

| 模块 | 职责 | 主要产物 |
|---|---|---|
| collector | 采集 executed BB、edge count、函数/循环、动态事件和累计指令数 | `dcfg.json.zst`、`events.zst` |
| matcher | 分层图匹配或 IRBB 精确匹配 | `anchor-map.json` |
| aligner | occurrence 归一化、单调序列对齐、区间构造 | `aligned-intervals.parquet` |
| projector/cluster | 映射已有 A slice，或构造 merged profile 统一聚类 | `regions.<build-id>.json`、per-build weights |
| checkpoint runner | 在 B 中按 marker occurrence 触发 checkpoint/ROI | B-native checkpoint 和日志 |

## 5. 详细实施算法

### 5.1 阶段 0：冻结 workload 身份

任何映射前先生成 manifest，至少保存：

- source commit/tree hash；
- workload 名称、输入文件 hash、参数和环境变量；
- thread count、CPU affinity、随机种子；
- compiler path/version、完整 flags、link command；
- ELF Build-ID 和 SHA-256；
- 静态库、动态库 Build-ID；
- OS/rootfs/firmware/DTB 标识；
- ASLR、PIE 和 record/replay 设置。
- `icount_domain`：目标进程/ASID、hart、线程、特权级，以及库、内核和其他 guest 任务是否计入。

硬性前置检查：

1. A/B 最终程序输出必须一致。
2. A/B 运行的目标输入和功能路径必须相同。
3. 同一 binary 重跑两次时，单线程 marker 序列 hash 应一致。
4. 多线程不一致时，必须引入确定性 record/replay，或把锚点提升为 barrier/task/work-item，而不是使用一个全局 occurrence 计数。

### 5.2 阶段 1：采集 DCFG 和轻量动态轨迹

在 NEMU/QEMU/原生插桩中，对每个执行过的 BB 记录：

- image Build-ID、image-relative PC 范围；
- 指令数和控制流后继；
- call/return/branch 类型；
- 所属函数、内联调用链、DWARF 文件/行/discriminator；
- loop header、loop nesting 和支配关系；
- 动态进入次数、edge traversal count；
- 目标进程/地址空间和逻辑线程 ID。

不要把 guest 虚拟 PC 单独当身份。PIE/ASLR 下至少使用 `(image Build-ID, image-relative offset)`；全系统运行还要限定目标进程/ASID，避免把共享库或另一个进程的相同 PC 计入 occurrence。

还必须固定“指令域”：SimPoint/BBV 的 progress 应只统计目标 workload 的用户态指令，还是包含系统调用、内核和其他 guest 任务。默认建议只统计目标进程用户态执行，并对系统调用单独记录；不能把 NEMU 从复位开始的全局 guest icount 与 workload interval icount 混用。

完整 edge trace 可能很大。建议采用两遍采集：

1. 第一遍只收集 DCFG 和动态计数，离线选出候选函数/循环 marker。
2. 第二遍只输出候选 marker 的有序事件流和累计 guest instruction count。

若 workload 无法确定性重跑，则第一遍就必须保存压缩完整 edge trace 或 record/replay 日志。2015 论文用相邻 edge 对 Huffman 编码，平均每十亿次 edge traversal 约 17.5 MB，但最大 workload 的 trace 仍达到约 16 GB。[2, PDF p.3]

### 5.3 阶段 2：构建并匹配静态语义锚点

优先级如下：

1. 共同 base IR 的稳定 `IRBB ID`；
2. 编译器生成的 source/IR marker ID；
3. 函数入口和 loop header 的源码身份；
4. DCFG 图匹配推导的函数/循环对应；
5. 纯 binary CFG fingerprint，仅作为低信息兜底。

建议 source marker ID 包含：

```text
normalized source path
+ demangled function signature
+ inline call-chain
+ lexical loop nesting path
+ source line/column/discriminator
+ event kind (function-entry / loop-header / loop-backedge)
```

对不同编译器产生的候选节点，计算综合分数：

```text
S_static = weighted(
  symbol similarity,
  source-span similarity,
  call-neighborhood similarity,
  loop-nesting similarity,
  CFG topology similarity,
  dynamic-count compatibility
)
```

不要用单一源码行作 ID：宏展开、同一行多个语句、内联和 loop peeling 都会造成冲突。

marker 机制不能反过来改变被研究的 binary。生产路径优先使用零指令 label、ELF note、DWARF 映射和模拟器 PC 监控，不在热循环中插可执行函数调用。若必须使用单独的 profiling build，应比较 release/alignment build 的 `.text` section hash 或逐函数机器码，确认 marker/debug 选项没有改变优化结果；否则 checkpoint 必须绑定实际被 profile 的 ELF，不能把位置再投给另一个“近似相同”的产物。

### 5.4 阶段 3：动态 occurrence 对齐

对每个静态锚点对 `(a,b)`，先用完整执行计数估计 occurrence 比例：

```text
r(a,b) = count_B(b) / count_A(a)
```

归一化执行进度 `k_A / count_A(a)` 只能用于产生 B 的候选 occurrence，不能直接作为最终映射。最终匹配还必须满足：

- A/B 事件顺序单调，不允许时间交叉；
- 候选两侧的调用/循环上下文一致；
- 局部 A/B 指令数比例与全程序或当前 phase 的比例相容；
- 前后若干 marker 的序列上下文相似；
- 所有选中 region 的起止锚点都能唯一命中。

可以沿用 2015 论文的滑窗启发式，但应输出所有回退信息。建议分别记录两种质量，不能混成一个 `confidence`：

- `anchor_match_confidence`：A/B 语义锚点本身是否可靠对应；
- `source_slice_fidelity`：映射区域对原 A 固定窗口的保真度，取 `exact`、`snapped`、`vli_union`、`interpolated` 或 `rejected`。

对非 exact 结果，还要记录 A 中两端移动的指令数、原窗口覆盖率和 interval IoU。建议把锚点匹配等级分为：

| 等级 | 边界来源 | 处理 |
|---|---|---|
| H | 相同 IR/source marker 的精确 occurrence | 允许正式 checkpoint |
| M | 高分 graph match，且动态序列精确命中 | 允许，但保留证据与复核 |
| L | 左右锚点吸附或插值 | 只允许实验性 checkpoint |
| R | 仅按总指令数比例或剩余长度推算 | 拒绝，转为 B 重新 profiling |

论文使用的 `0.2` graph/dynamic score cutoff 是特定实验的启发式参数，不能直接作为本项目的质量门槛。实际阈值要用 A->A self-map 和已知源级对应的 validation set 校准。

### 5.5 阶段 4A：映射已有 A 切片

假设 A 的 SimPoint interval index 为 `p`，interval size 为 `L`。只有在 manifest 中确认 A profiling 使用的 `icount_domain` 后，才可按当前 profiler 的零基/一基和边界语义计算：

```text
I_A_start = p * L
I_A_end   = (p + 1) * L
```

不能在未核对实现的情况下默认公式，尤其要确认目标进程/ASID、hart/thread、用户态/内核态、库代码是否计数，以及 checkpoint 是在边界指令执行前还是执行后触发。

对每个 A slice：

1. 在 A 的对齐事件链中找到 start/end 左右的公共 marker。
2. 若 start/end 正好命中 marker，直接取其 B 对应事件。
3. 若没有精确命中，可选择与 `[I_A_start,I_A_end)` 重叠最大的连续公共 VLI 集合，并把对应 B 区间作为一个**新实验区域**；记录 A 中两端位移、覆盖率和 IoU，不得标为 exact reuse。
4. 如果必须保持近似固定长度，可在左右 marker 间插值，但将 `source_slice_fidelity` 标为 `interpolated`，只用于实验验证。
5. 若任一边界附近缺少公共 marker、候选不唯一或映射次序交叉，拒绝该 slice。

伪代码：

```text
for region_A in selected_A_regions:
    start_bracket = aligned_events.around(region_A.start)
    end_bracket   = aligned_events.around(region_A.end)

    start_B, ev_s, fidelity_s = project_or_snap(start_bracket)
    end_B,   ev_e, fidelity_e = project_or_snap(end_bracket)

    anchor_confidence = combine_anchor_evidence(ev_s, ev_e, monotonicity, context)
    source_fidelity = combine_slice_fidelity(fidelity_s, fidelity_e)
    if anchor_confidence == rejected
       or source_fidelity == rejected
       or ev_s.global_event_seq >= ev_e.global_event_seq:
        require_B_reprofiling(region_A)
    else:
        emit_target_region(start_B, end_B, anchor_confidence, source_fidelity)
```

这条兼容路线只用于生成 B 的候选 checkpoint，不声称恢复 B 的全程序权重。旧 SimPoint cluster label 属于 A 的 fixed-length partition，不能直接赋给新公共 VLI。若需要权重和 speedup，必须在 A 的公共 VLI BBV 上重新运行 SimPoint（2007 路线），或直接进入 A/B merged-profile 联合选区。

### 5.6 阶段 4B：联合构造 SimPoint 输入（推荐）

先用公共边界把 A/B 划为数量相同的 VLI。对第 `j` 个对齐 interval，分别生成 canonical source/IR vector 或各自 BBV，并构造：

```text
V_j = concat(normalize(V_A,j), normalize(V_B,j))
```

两个 binary 的特征块应分别归一化并等权，避免动态指令更多或 BB 维度更多的一方主导距离。然后对 `V_j` 统一运行 SimPoint，得到共同 cluster label 和代表 interval ID。

这一阶段不能把 VLI 文件直接冒充现有固定长度 BBV。聚类和代表点选择必须读取每个 build 的实际 interval length；如果当前 SimPoint wrapper 不支持 VLI，需要增加 length sidecar/加权路径。此后 `simpoints0` 中的 point 应解释为 `aligned interval ID`，checkpoint 生成器必须通过 `aligned-intervals` 查 start/end marker，不能再计算 `point * interval_size`。

对每个 binary `X`，cluster `c` 的指令权重为：

```text
weight_X(c) =
  sum(inst_X(j) for interval j with label(j)=c)
  / sum(inst_X(j) for all intervals j)
```

因此 A/B 使用相同代表区域 ID，但实际 start/end PC、动态长度和 weight 都是各自的。

### 5.7 阶段 5：在 B 上生成 checkpoint

已有 `simpoints0` 通常只定义 ROI，并不自动给出 warmup。建议每个 region 最多保存三个边界：

- `warmup_start`：创建 architectural checkpoint 或开始 warmup；
- `roi_start`：打开统计；
- `roi_end`：关闭统计并结束该 region。

默认可以在 mapped `roi_start` 创建 checkpoint，此时不声明已经完成微架构 warmup。若需要 warmup，应在对齐 interval 链上向前选择明确数量或明确语义的完整 VLI，并独立映射其 `warmup_start`；不能用 `roi_start - 固定指令数` 推导 B 的 warmup 位置。

目标 region plan 示例：

```json
{
  "schema_version": 1,
  "region_id": "cluster-3",
  "reference": {
    "elf_build_id": "A_BUILD_ID",
    "simpoint": 14492,
    "interval_size": 20000000
  },
  "target": {
    "elf_build_id": "B_BUILD_ID",
    "weight": 0.074972
  },
  "boundaries": {
    "warmup_start": {
      "anchor_id": "loop:source-id-17",
      "occurrence": 9181,
      "module_offset": "0x1234",
      "global_event_seq": 1840021,
      "event_kind": "loop_header",
      "instruction_offset_in_bb": 0,
      "trigger": "before_instruction"
    },
    "roi_start": {
      "anchor_id": "loop:source-id-22",
      "occurrence": 9214,
      "module_offset": "0x18a0",
      "global_event_seq": 1841790,
      "event_kind": "loop_header",
      "instruction_offset_in_bb": 0,
      "trigger": "before_instruction"
    },
    "roi_end": {
      "anchor_id": "func:source-id-8",
      "occurrence": 407,
      "module_offset": "0x2910",
      "global_event_seq": 1869970,
      "event_kind": "function_entry",
      "instruction_offset_in_bb": 0,
      "trigger": "before_instruction"
    }
  },
  "alignment": {
    "method": "graph-anchor-exact",
    "anchor_match_confidence": "M",
    "source_slice_fidelity": "exact"
  },
  "checkpoint_runtime": {
    "counter_state_saved": true,
    "capture_event_consumed": true,
    "resume_stage": "warmup"
  }
}
```

这些字段是建议 schema，不是现有工具已经支持的命令或格式。

marker event 需要明确事件类型和时刻。函数入口/loop header 可在目标指令执行前触发；loop backedge 必须记录具体 branch、taken 条件，并选择 `before_instruction`、`after_taken_edge` 或 `before_target_bb`，不能统一简化为 `before_bb`。ROI 一律定义成半开动态区间 `[roi_start, roi_end)`。

NEMU/QEMU 侧的触发逻辑可抽象为：

```text
if current_process == target_process
   and current_module_build_id == target_build_id
   and pc == mapped_marker_pc:
       occurrence[anchor_id] += 1
       if occurrence[anchor_id] == requested_occurrence:
           checkpoint_or_toggle_roi_at_declared_event_semantics()
```

不能以 `guest_icount == A_icount` 触发 B。`guest_icount` 只能作为定位和超时诊断提示。

checkpoint 必须同时持久化 alignment runtime state：每个相关 anchor counter、统一的 `global_event_seq`、当前 one-shot stage，以及 capture marker 是否已经消费。restore 后从 capture 指令之后重新 arm，避免把同一 marker 再计一次；如果插件状态不在 simulator checkpoint 内，就把它写入 checkpoint sidecar 并在 restore 时显式加载。否则从零开始计数将无法命中绝对 occurrence，重复计数又会产生 off-by-one。

## 6. 数据产物与目录建议

每次运行使用独立 namespace，避免不同 ELF 的数据混淆：

```text
alignment/<workload>/<input-id>/
  manifest.json
  builds/
    <A-build-id>/
      elf.json
      dcfg.json.zst
      anchor-catalog.json
      events.zst
      bbv.gz
    <B-build-id>/
      ...
  maps/
    A-to-B.anchor-map.json
    A-to-B.aligned-intervals.parquet
    A-to-B.quality.json
  regions/
    <A-build-id>.json
    <B-build-id>.json
  checkpoints/
    <B-build-id>/<region-id>/...
  logs/
```

所有 checkpoint 元数据必须绑定 B ELF Build-ID、输入 hash、rootfs/firmware/DTB 和 marker plan hash。否则以后很容易把“语义区域相同”误当成“checkpoint 二进制兼容”。

## 7. 验证方案

验证必须分三层，不能只看 checkpoint 文件是否生成。

### 7.1 对齐正确性

| 检查 | 通过条件 |
|---|---|
| A->A self-map | 所有边界、occurrence 和 interval ID 完全一致，解决零基/一基及 before/after off-by-one |
| 重复运行确定性 | 同一 binary 的 marker sequence hash、程序输出和关键计数一致 |
| 静态映射 | 无一对多未决歧义；函数/循环上下文合理 |
| 动态映射 | 事件配对全局单调，无 crossing，边界均唯一命中 |
| 区间语义 | canonical IR/source vector 相似；调用/循环序列一致 |
| 回退审计 | 每个边界明确记录 exact、snapped、vli_union、interpolated 或 rejected |
| 原切片保真度 | exact reuse 两端位移均为 0；其他结果报告覆盖率、IoU 和位移 |

### 7.2 Checkpoint 正确性

每个 B checkpoint 至少验证：

1. 能被目标模拟器实际 restore，而不只是压缩文件可读。
2. restore 后依次命中 `roi_start`、`roi_end` 和 workload 正常退出标志。
3. 与从头运行 B 相比，ROI 内架构指令数、关键 marker 次数和程序输出一致。
4. checkpoint 中的 ELF、地址空间、库、固件和设备状态全部属于 B。
5. 多次生成同一 region 时，允许的非确定性和 hash 差异有明确说明。
6. restore 后 marker counters、`global_event_seq` 和 one-shot stage 正确续接，capture event 不重复消费。

### 7.3 代表性与性能比较

映射正确不代表 SimPoint 仍能代表 B。应在一组可完整运行的 validation workloads 上比较：

- B 全程序 ground truth；
- B 独立 SimPoint；
- 2007 式 primary-A VLI mapping；
- 2015 式 merged-profile joint selection；
- 若适用，再加入 IRBB/Nugget 路线。

至少报告：

- speedup/metric relative error 的 mean、median、P95、max；
- exact boundary rate、interpolation rate、reject rate；
- 公共 marker 覆盖率和最大 marker gap；
- B/A interval-length ratio 分布；
- trace 大小、profiling slowdown、匹配时间；
- checkpoint restore 成功率及终端完成标志。

建议先把以下设为硬门槛：

- A->A self-map 100% 精确；
- 所有正式 checkpoint 的边界都是 H/M 级，禁止纯比例回退；
- 所有正式 checkpoint 100% restore 并完整命中 ROI 协议；
- validation set 中程序输出与 full run 一致。

性能误差门槛应按项目用途设定，不直接照抄论文的 0.69% 平均误差。可先把“平均 cross-binary speedup error 不高于 1%，P95 不高于 3%”作为试运行目标，再依据 RISC-V workload 的实测分布调整，而不是把它当论文保证。

## 8. 失败场景与回退

| 失败场景 | 原因 | 正确处理 |
|---|---|---|
| 只有 A/B ELF 和 A 的 point number | 缺少动态语义上下文 | 重跑 A/B 采集 DCFG/marker trace |
| A/B 输出或控制流不同 | 不是同一语义 workload | B 独立 profiling/cluster |
| 公共 marker 距离远大于目标 slice | 优化破坏了可映射结构 | 图匹配增强；仍不足则 B 重选点 |
| 多个同分候选 | 同构循环、模板、宏或内联歧义 | 加调用上下文、源码 discriminator、前后事件序列；不能消歧则 reject |
| 动态事件次序 crossing | 匹配错误或运行不确定 | 删除低分对；若影响边界则 reject |
| 映射集合新增一个 target build | 全体公共 marker 的交集可能改变 | 重新计算所有 build 的公共锚点和 aligned intervals |
| 只能比例插值 | 没有可证明的语义边界 | 仅作为探索性结果，不生成正式集 |
| B checkpoint 可生成但 restore 失败 | B 的系统状态/镜像契约不一致 | 修复 checkpoint 生成环境，不归因于 mapping 成功 |
| 映射准确但性能误差大 | A 样本不代表 B 的新增行为 | 使用 merged profile 重新统一选点 |

## 9. 分阶段实施计划

### M0：可复现实验集和协议

- 选 3-5 个单线程、确定性 workload；固定输入和运行环境。
- 准备难度递增的 build pair：相同 ELF、同编译器不同 ISA flag、不同优化级别、相邻 GCC/Clang 版本、不同编译器。
- 固定各 `event_kind` 的触发语义、ROI 半开区间和 `simpoints0` 索引规则。
- 产出 manifest schema 和 A->A self-map 基线。

完成标准：相同 ELF 重跑的动态 marker 序列稳定，A->A 映射无 off-by-one。

### M1：高置信度 marker MVP

- 先实现函数入口和 loop header catalog。
- 使用 DWARF/source identity + 动态 occurrence 匹配。
- 正式集只接受原 A 起止均 exact；snap/VLI union 只生成带位移与 IoU 的实验计划，不做比例回退。
- 让 NEMU/QEMU 根据 B marker plan 生成一个 B-native checkpoint。

完成标准：至少一组 `-O2`/`-O3` 或 ISA flag 不同的 ELF，B checkpoint 能 restore、命中 ROI 并正常退出。

### M2：DCFG 图匹配与序列对齐

- 加入 call graph、loop dominance graph 和综合 affinity。
- 实现动态 count ratio、滑窗单调对齐、crossing 消除和置信度报告。
- 支持复杂内联、loop unroll/vector width 变化。

完成标准：相较 M1 提高公共 marker 覆盖率，且 validation set 不增加错误匹配。

### M3：联合 VLI/SimPoint

- 生成 A/B 对齐 VLI 和 merged feature vectors。
- 统一运行 SimPoint。
- 输出 per-build length、cluster label、representative region 和 weight。

完成标准：与 B full-run ground truth 比较，cross-binary speedup error 优于或不劣于独立 SimPoint 基线，并报告尾部误差。

### M4：生产化 checkpoint 集成

- 支持 warmup/start/end 三 marker 协议。
- 绑定 Build-ID、输入、镜像、DTB 和 plan hash。
- 加入批量并行、超时、恢复验证和失败重试 namespace。

完成标准：批量输出中每个正式 checkpoint 都有映射证据、restore 证据和 workload 终止证据。

### M5：IRBB 快速路径与多线程扩展

- 对能统一到 canonical LLVM IR 的 workload 加 Nugget 式 IRBB marker。
- 对多线程 workload 引入 record/replay 或 barrier/task 级逻辑时间。
- 不把单线程 occurrence 算法直接扩展成全局多线程计数。

## 10. 最终建议

针对“配置 A 已经有切片，如何映射到配置 B”这一具体问题，建议先实现兼容模式：

1. 保留 A 的原始 ELF、interval size、`simpoints0` 和输入。
2. 用 A/B 各跑一次相同输入，采集 DCFG、函数/循环 marker 计数和有序事件流。
3. 以 A 为 reference 做分层图匹配和动态序列对齐。
4. 仅当 A 起止都精确命中时称为原切片复用；否则把公共 VLI/锚点括住的结果标为新实验区域，并记录位移、覆盖率与 IoU。
5. 输出 B 的 `(marker PC, occurrence)`，而不是 A/B 指令数比例。
6. 从头运行 B，在对应 occurrence 生成 B-native checkpoint。
7. 兼容模式不复用旧 FLI label 计算 B 权重；若用于全程序估计，重跑公共 VLI SimPoint 或切换到联合 VLI/SimPoint 流程。

第一版应明确限制为“同源、同输入、单线程、确定性、可完整功能运行、最好带 DWARF”。这不是保守措辞，而是三篇论文共同依赖的语义一致性基础。把这一闭环验证后，再扩展到 stripped ELF、复杂库和多线程。

## 参考文献

[1] E. Perelman, J. Lau, H. Patil, A. Jaleel, G. Hamerly, and B. Calder, “Cross Binary Simulation Points,” *IEEE ISPASS*, pp. 179-189, 2007, DOI: `10.1109/ISPASS.2007.363748`. 本地文件：`Cross_Binary_Simulation_Points (1).pdf`。

[2] C. Yount, H. Patil, M. S. Islam, and A. Srikanth, “Graph-Matching-Based Simulation-Region Selection for Multiple Binaries,” *IEEE ISPASS*, pp. 52-61, 2015, DOI: `10.1109/ISPASS.2015.7095784`. 本地文件：`Graph-matching-based_simulation-region_selection_for_multiple_binaries (1).pdf`。

[3] Z. Qiu, M. Samani, and J. Lowe-Power, “Nugget: Portable Program Snippets,” *IEEE HPCA*, 2026, DOI: `10.1109/HPCA68181.2026.11408606`. 本地文件：`Nugget_Portable_Program_Snippets.pdf`。
