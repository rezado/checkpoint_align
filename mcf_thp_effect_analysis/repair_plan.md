# mcf THP 修复计划

## 已确认的现象

- `mcf_A` 是 THP on，`mcf_B` 是 THP off；13 个切片中 7 个切片在 THP on 下周期增加，SimPoint 加权周期增加 2.08%，IPC 下降 1.50%。
- THP on 的跨切片 TLB miss、PTW request/response 和 PTW-originated L2 miss 分别下降约 52%、76% 和 93%。地址转换收益已经出现。
- THP on 的 `sp_hit/spRefill` 从 8,761/133 增加到 3.26M/3.38M。大页命中和 refill 同时很高，说明 superpage 工作集或 page-level 冲突正在消耗 PTW cache。
- `mcf_4019` 是主要回退切片：周期 +16.80%。它的 TLB miss 从 17.61M 降到 11.06M，PTW request 从 9.26M 降到 2.87M，但 `MemNotReadyStall` 从 2.23M 增到 92.13M，`LoadL2Stall` 从 2.57M 增到 19.05M。D-cache miss 基本不变，L2 demand miss 下降 43.9%。
- mcf 日志中的 BitmapCheck 计数器为 0，因此这批实验不能证明 bitmap 检查是性能原因。

## 修复顺序

### 1. 固定实际硬件配置并扫描 superpage cache

源码中 `MinimalConfig` 的 `spSize` 是 4，而 `DefaultConfig` 通过 `L2TLBParameters()` 使用默认值 16。当前 mcf 日志引用 `DefaultConfig`，必须先从最终生成的 elaboration/config manifest 确认实际值，不能只改未使用的 `MinimalConfig`。

在同一 DefaultConfig 基线下扫描：

```text
spSize = 4, 8, 16, 32
```

每个点固定 emulator、reference model、seed、节点和 checkpoint，记录：`sp_hit`、`spRefill`、`ptw_req_count`、`MemNotReadyStall`、`LoadL2Stall`、周期和 IPC。第一目标是让 `spRefill/access` 明显下降，同时不能牺牲 `sp_hit` 延迟。

如果扩大容量有效，再检查 2MiB、1GiB、512GiB 三种 level 是否共享同一组 entry。必要时按 level 分区，避免一个大页 level 驱逐另一个 level 的热点 entry。`PageTableCache` 当前用同一个 `spreplace` 选择 refill way。

### 2. 验证 superpage refill 和 TLB refill 的并行性

在 `PageTableCache`、`L2TLB` 和 `PageTableWalker` 增加以下计数器：

- superpage hit 后直接返回的请求数；
- superpage hit 后转入 LLPTW/PTW 的请求数；
- refill 等待周期、miss queue occupancy、同一 VPN 的合并请求数；
- superpage refill 后 L1 DTLB refill 的请求数。

然后分别测试 `refillBothTlb=false/true` 和 `llptwsize=6/8/16`。如果 `MemNotReadyStall` 随 LLPTW occupancy 上升，优先扩大并行度或合并同 VPN 请求；如果主要是 L1 DTLB 未及时 refill，则修复 refill/bypass 时序。

### 3. 检查物理地址造成的 L2/DRAM 映射变化

THP 改变 PPN，即使虚拟地址和 ELF 相同，也会改变物理 cache tag、L2 slice/set/bank 以及 DRAM row。对 `4019` 增加按物理地址记录的：

- L2 slice/set/bank 分布；
- MSHR occupancy 和 request latency histogram；
- DRAM row hit/conflict；
- prefetch request、hit、drop 和 TLB-not-ready drop。

若 `spSize` 扫描无效而 L2 bank/row 偏斜明显，采用 2MiB page coloring 或选择性 THP，只对连续且热点、不会集中到少数 L2 bank 的区域做 promotion。

### 4. 修正实验和计数器语义

- 两组必须使用同一 emulator build、reference model、seed 和宿主节点，至少重复 3 到 5 次。
- A/B 必须使用相同 measured instruction window；`simulator_out` 的完整 40M 指令 cycle/IPC 与 warmup reset 后的 `[PERF]` 窗口不能直接相加。
- 计数器导出要使用 64-bit unsigned 累加，过滤 `mean/sample/histogram` 派生项；对异常负值直接报警，不参与机制归因。

## XiangShan 相关代码位置

- superpage entry、hit 和 refill：`src/main/scala/xiangshan/cache/mmu/PageTableCache.scala`
- L1 TLB superpage match/PPN 拼接：`src/main/scala/xiangshan/cache/mmu/MMUBundle.scala`
- PTW leaf/refill 状态机：`src/main/scala/xiangshan/cache/mmu/PageTableWalker.scala`
- L2 TLB/LLPTW arbitration：`src/main/scala/xiangshan/cache/mmu/L2TLB.scala`
- Default/Minimal 配置：`src/main/scala/top/Configs.scala`、`src/main/scala/xiangshan/cache/mmu/MMUConst.scala`
