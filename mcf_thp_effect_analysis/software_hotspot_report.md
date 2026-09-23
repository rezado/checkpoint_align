# mcf 关键切片软件热点与计数器来源

本报告把 THP on（目录 `mcf_A`）和 THP off（目录 `mcf_B`）的最终 `[PERF]` 快照，与 B build 的带 DWARF `mcf.elf`、对齐 checkpoint sidecar 以及 XiangShan 源码对应起来。

## 数据口径

- A = THP off，来自原始目录 `mcf_B`；B = THP on，来自原始目录 `mcf_A`。
- 计数器是 `simulator_err.txt` 中 warmup reset 后的最终 `[PERF]` 快照。
- 周期/IPC 来自 `simulator_out.txt` 的 40M 指令窗口。
- 原始逐切片数据：[slice_performance.csv](slice_performance.csv)、[counter_comparison.csv](counter_comparison.csv)。
- 原始性能摘要：`mcf_A/<slice>/simulator_out.txt`（THP on）和 `mcf_B/<slice>/simulator_out.txt`（THP off）。
- 原始计数器快照：`mcf_A/<slice>/simulator_err.txt` 和 `mcf_B/<slice>/simulator_err.txt`，取最大 `[PERF time=...]` 的记录；解析逻辑见 [`mcf_thp_analysis.py`](../mcf_thp_analysis.py:35)。
- B build ELF：[mcf.elf](../data/results/spec06_gcc16_rva23_novec_g_260916_mcf_aligned/elf/mcf.elf)。
- 动态 PC 和源码语义位置来自：[alignment-map.json](../data/results/spec06_gcc16_rva23_novec_g_260916_mcf_aligned/provenance/alignment-map.json) 和对应 `provenance/sidecars/point-*.json`。

## 关键切片

| 切片 | 权重 | THP on 周期变化 | THP on IPC 变化 | 对齐源码位置 | 动态 PC |
|---|---:|---:|---:|---|---|
| `mcf_4019` | 0.180498 | +16.80% | -14.39% | `pbeampp.c:42/45`, `bea_is_dual_infeasible` | `0x1a47a` |
| `mcf_7363` | 0.059896 | +7.17% | -6.69% | `mcfutil.c:89` | `0x1aba0` |
| `mcf_5049` | 0.144169 | +3.15% | -3.05% | `implicit.c:168` | `0x1a8c8` |
| `mcf_1773` | 0.005402 | +7.44% | -6.93% | `implicit.c:167` | `0x1a8b8` |

`4019` 是最重要的回退点：单切片周期增加 8.93M，按 SimPoint 权重贡献约 +3.03 个百分点。`7363` 的周期增加 4.41M，贡献约 +0.43 个百分点。`5049` 虽然回退幅度较小，但权重较高，贡献约 +0.45 个百分点。

## 软件热点解释

### `mcf_4019`: `pbeampp.c:42/45`

DWARF 将 checkpoint 对齐点绑定到 `primal_bea_mpp()` 内的 `bea_is_dual_infeasible` 判断。对应反汇编位于 `mcf.elf` 的 `0x1a47a` 附近：先检查候选弧的 reduced-cost/可行性条件，再进入 `pbeampp.c:165` 的 basket 扫描。该路径是 MCF 的 pointer-heavy 链表/结构体访问：节点、`cost`、`pred`、`flow` 等字段通过多级指针读取，适合解释 load 依赖链对地址布局很敏感。

本切片的 THP on 原始计数器：

| 计数器 | XiangShan 源码定义 | THP off | THP on | 变化 |
|---|---|---:|---:|---:|
| `tlb_miss` | [`NewLoadUnit.scala:788`](../../XiangShan/src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:788) | 17,614,608 | 11,058,705 | -37.22% |
| `ptw_req_count` | [`Repeater.scala:342`](../../XiangShan/src/main/scala/xiangshan/cache/mmu/Repeater.scala:342) | 9,261,256 | 2,869,060 | -69.02% |
| `ptw_resp_count` | [`Repeater.scala:675`](../../XiangShan/src/main/scala/xiangshan/cache/mmu/Repeater.scala:675) 等实例汇总 | 36,945,003 | 11,470,357 | -68.95% |
| `sp_hit` | [`PageTableCache.scala:1301`](../../XiangShan/src/main/scala/xiangshan/cache/mmu/PageTableCache.scala:1301) | 2,866 | 1,197,254 | +41,674.39% |
| `spRefill` | [`PageTableCache.scala:1381`](../../XiangShan/src/main/scala/xiangshan/cache/mmu/PageTableCache.scala:1381) | 3 | 1,228,081 | +40,935,933.33% |
| `l1DemandMiss` | [`PrefetcherMonitor.scala:111`](../../XiangShan/src/main/scala/xiangshan/mem/prefetch/PrefetcherMonitor.scala:111) | 634,980 | 2,156,071 | +239.55% |
| `l2demandMiss` | 运行日志中的 L2 demand miss 实例 | 739,553 | 415,185 | -43.86% |
| `dcache_miss` | [`NewLoadUnit.scala:1248`](../../XiangShan/src/main/scala/xiangshan/mem/pipeline/NewLoadUnit.scala:1248)，3 个 LoadUnit 实例汇总 | 23,127,474 | 23,491,548 | +1.57% |
| `LoadL2Stall` | [`Rob.scala:1553`](../../XiangShan/src/main/scala/xiangshan/backend/rob/Rob.scala:1553) | 2,569,545 | 19,054,005 | +641.53% |
| `MemNotReadyStall` | [`Rob.scala:1554`](../../XiangShan/src/main/scala/xiangshan/backend/rob/Rob.scala:1554) | 2,234,082 | 92,127,363 | +4,023.72% |
| `backend_stall_cycle` | XiangShan backend top-down counter | 24,256,641 | 28,181,908 | +16.18% |
| `branchMispredicts` | XiangShan branch predictor counter | 19,439 | 9,711 | -50.04% |

关键含义是：`4019` 的页表转换明显改善，但 L1 demand miss 和 ROB 头部 load 等待显著恶化；L2 demand miss 反而下降，说明瓶颈更像是关键 pointer-chasing load 的 ready/依赖时序和物理地址映射变化，而非总的页表流量。

### `mcf_7363`: `mcfutil.c:89`

该点位于 MCF utility 路径。计数器方向与 `4019` 相同但幅度较小：TLB miss `12,477,951 -> 8,930,658`，PTW request `6,572,473 -> 2,619,779`，同时 `l1DemandMiss` 增加 `74.46%`，`MemNotReadyStall` 增加 `10.45x`，`LoadL2Stall` 增加 `216.97%`。这说明软件阶段虽不同，THP on 后都出现“地址转换收益换来关键 load 就绪变差”的模式。

### `mcf_5049`: `implicit.c:168`

该点的页表收益几乎饱和：TLB miss 和 PTW request 均下降约 `99.93%`。但 L1 demand miss 增加 `27.67%`，L2 demand miss 增加 `48.66%`，D-cache miss 增加 `24.52%`，周期仍增加 `3.15%`。它更像是 THP 引入的物理页/cache 映射代价，而不是 superpage refill 造成的 ROB 长等待：`MemNotReadyStall` 反而下降 `2.48%`，`LoadL2Stall` 基本不变。

### `mcf_1773`: `implicit.c:167`

该点 TLB/PTW 收益最大（TLB miss `-99.31%`，PTW request `-99.51%`），但 L1 demand miss 增加 `52.90%`，`MemNotReadyStall` 增加 `24.53%`。`LoadL2Stall` 下降 `9.18%`，D-cache miss 下降 `16.26%`，因此这个低权重点的回退更可能是前端/依赖时序变化，不能简单归咎于 L2 miss。

## 计数器来源总表

| 计数器 | 来源文件/行 | 统计条件或语义 |
|---|---|---|
| `tlb_miss` | `NewLoadUnit.scala:788` | load pipeline fire 且 `tlbMiss` |
| `tlb_miss_first_issue` | `NewLoadUnit.scala:789` | 首次 issue 的 TLB miss |
| `dcache_miss` | `NewLoadUnit.scala:1248` | LoadUnit fire 且 D-cache response 标记 miss |
| `l1DemandMiss` | `PrefetcherMonitor.scala:111` | 各 LDU demand miss 的 PopCount |
| `tlb_req_count` | `Repeater.scala:337/666` | TLB request valid 数量 |
| `ptw_req_count` | `Repeater.scala:342/671` | PTW request fire |
| `ptw_resp_count` | `Repeater.scala:675`、`PageTableWalker.scala:690` | PTW response fire |
| `sp_hit` | `PageTableCache.scala:1301` | PageTableCache superpage response hit |
| `spRefill` | `PageTableCache.scala:1027`、`1381` | superpage leaf PTE 实际 refill |
| `LoadL2Stall` | `Rob.scala:1551-1554` | ROB head load 被判定为 L1 miss，等待 L2 |
| `MemNotReadyStall` | `Rob.scala:1543-1555` | ROB head memory instruction 尚未 issue-ready |

## 限制

当前没有拿到 benchmark C 源文件本体，软件分析依赖 ELF 内嵌 DWARF、`objdump -dS` 和对齐 sidecar 的源码路径/行号；因此可以可靠定位到函数、源码行和指令区间，但不能在本仓库直接展示完整原始 C 代码。两组运行还使用了不同 seed、checkpoint 路径和 reference model build，以上结果是机制证据，不是已经消除所有混杂因素的严格因果实验。
