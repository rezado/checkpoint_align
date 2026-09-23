# mcf THP 性能影响分析（配置标注已对调）

- **A：未开启 THP**（原始目录 `mcf_B`）。
- **B：开启 THP**（原始目录 `mcf_A`）。
- 本报告的变化量统一为 `B - A`，即“开启 THP 相对未开启 THP”。正的周期/停顿变化表示 THP 开启后增加，正的 IPC 变化表示 THP 开启后提升。
- 计数器来自每个切片的最终 `[PERF]` 快照；warmup 后计数器已 reset；重复的路径/计数器实例保留为 `#instance_N`。

## 结论

- THP 开启后，**7/13 个切片周期数增加**；周期变化中位数 **+0.12%**，按 SimPoint 权重加权 **+2.08%**。
- IPC 变化中位数 **-0.12%**，按权重加权 **-1.50%**；主机仿真时间按权重变化 **+21.60%**（仅作辅助参考）。
- 这不是“THP 让所有地址转换指标都变差”：THP 显著减少了 TLB miss 和页表遍历，但 superpage cache refill 很高；本次 workload 的总执行时间主要受后端/访存就绪和 L2 访问行为影响，地址转换收益没有抵消这些代价。

## 归因边界

- 这组数据支持“THP 开启时出现了以下计数器变化”的结论，但还不能把全部周期差异严格归因于 THP 本身：原始日志的随机 seed 不同，且两组日志的 emulator 编译时间/参考模型路径不同。
- 输入镜像路径也不同：原始 `mcf_A` 使用路径名含 `thp_g` 的 checkpoint，原始 `mcf_B` 使用路径名含 `novec_g` 的 checkpoint。因此，若要做严格因果结论，应使用同一 emulator build、同一 seed/重复多 seed，并只切换 THP 配置后重复运行。

## 为什么 THP 开启后仍可能变慢

1. **地址转换收益明确存在，但不是关键瓶颈。** TLB miss、TLB request 和 PTW request/response 均下降，说明大页减少了页表层级遍历。
2. **superpage cache 仍承受高 refill 压力。** THP on 下 superpage hit 和 refill 同时达到高位；应先确认最终 elaborated 的 `spSize`，再做容量和 page-level 分区扫描。
3. **数据访问行为发生变化。** L1 demand miss 增加，而 L2 demand miss 下降；这说明 THP 改变了页/地址布局和缓存索引映射，必须结合 L2 set/slice、MSHR 和 latency 数据判断。
4. **后端等待成为主要代价。** `MemNotReadyStall`、`LoadL2Stall` 等计数器增加；这些 top-down 计数器不是可加的延迟分解，当前证据支持关键 load 就绪/依赖时序变差。
5. **不是分支预测导致。** branch mispredicts 下降，性能下降更像是内存层级/后端就绪状态变化，而不是控制流恶化。

## 关键计数器

| 类别 | 计数器 | 覆盖切片 | A 未开启 THP | B 开启 THP | THP 开启相对变化 |
|---|---|---:|---:|---:|---:|
| address-translation pressure | `tlb_miss` | 13 | 62,237,913 | 29,652,376 | -52.36% |
| address-translation pressure | `tlb_req_count` | 13 | 63,276,845 | 29,796,524 | -52.91% |
| page-table-walk traffic | `ptw_req_count` | 13 | 35,279,803 | 8,426,630 | -76.11% |
| page-table-walk traffic | `ptw_resp_count` | 13 | 140,590,731 | 33,666,589 | -76.05% |
| superpage-cache activity | `sp_hit` | 13 | 8,761 | 3,260,770 | +37119.15% |
| superpage-cache activity | `spRefill` | 13 | 133 | 3,379,024 | +2540519.55% |
| page-table-walk traffic | `E2_L2AReqSource_PTW_Miss` | 13 | 743,850 | 49,201 | -93.39% |
| data-cache pressure | `l1DemandMiss` | 13 | 19,194,864 | 25,891,253 | +34.89% |
| data-cache pressure | `l2demandMiss` | 13 | 4,547,193 | 3,980,256 | -12.47% |
| data-cache pressure | `dcache_miss` | 13 | 129,046,758 | 127,678,245 | -1.06% |
| memory-system activity | `mem_cycle` | 13 | 198,204,001 | 111,680,783 | -43.65% |
| load latency | `LoadL2Stall` | 13 | 86,333,052 | 120,651,089 | +39.75% |
| load latency | `LoadL3Stall` | 13 | 63,543,565 | 45,394,186 | -28.56% |
| load latency | `LoadMemStall` | 13 | 439,566,212 | 315,507,731 | -28.22% |
| backend readiness | `MemNotReadyStall` | 13 | 55,023,925 | 263,960,139 | +379.72% |
| backend readiness | `backend_stall_cycle` | 13 | 174,942,455 | 182,005,850 | +4.04% |
| backend readiness | `exec_stall_cycle` | 13 | 134,156,065 | 142,639,549 | +6.32% |
| front-end readiness | `stallCycles_fetch_ifuNotReady` | 13 | 160,350,205 | 175,524,279 | +9.46% |
| front-end readiness | `stallCycles_ibufferFull` | 13 | 163,914,343 | 178,801,618 | +9.08% |
| front-end/backend queues | `stall_cycle_iq` | 13 | 124,529,957 | 134,201,600 | +7.77% |
| front-end/backend queues | `stallCycles_decodeFull` | 13 | 172,119,143 | 179,414,204 | +4.24% |
| control flow | `branchMispredicts` | 13 | 1,097,430 | 1,085,346 | -1.10% |

## 逐切片性能

| 切片 | 权重 | A cycles | B cycles | THP 开启周期变化 | A IPC | B IPC | THP 开启 IPC 变化 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `mcf_1773_0.00540212` | 0.005402 | 39,976,989 | 42,951,457 | +7.44% | 1.000576 | 0.931284 | -6.93% |
| `mcf_2709_0.335742` | 0.335742 | 71,358,964 | 68,624,579 | -3.83% | 0.560546 | 0.582882 | +3.98% |
| `mcf_4019_0.180498` | 0.180498 | 53,112,335 | 62,037,783 | +16.80% | 0.753121 | 0.644768 | -14.39% |
| `mcf_5049_0.144169` | 0.144169 | 16,280,860 | 16,793,812 | +3.15% | 2.456873 | 2.381830 | -3.05% |
| `mcf_5466_0.103316` | 0.103316 | 19,399,697 | 18,739,393 | -3.40% | 2.061888 | 2.134541 | +3.52% |
| `mcf_7363_0.059896` | 0.059896 | 61,586,061 | 65,999,041 | +7.17% | 0.649498 | 0.606069 | -6.69% |
| `mcf_8719_0.00175569` | 0.001756 | 55,604,633 | 55,362,612 | -0.44% | 0.719365 | 0.722509 | +0.44% |
| `mcf_8725_0.00513201` | 0.005132 | 28,594,285 | 28,628,350 | +0.12% | 1.398881 | 1.397217 | -0.12% |
| `mcf_8837_0.0355865` | 0.035587 | 34,674,340 | 34,844,203 | +0.49% | 1.153591 | 1.147967 | -0.49% |
| `mcf_9417_0.03822` | 0.038220 | 22,592,900 | 21,836,043 | -3.35% | 1.770468 | 1.831834 | +3.47% |
| `mcf_10311_0.0726585` | 0.072659 | 61,522,702 | 62,598,173 | +1.75% | 0.650167 | 0.638996 | -1.72% |
| `mcf_11026_0.00877845` | 0.008778 | 32,549,989 | 32,409,739 | -0.43% | 1.228879 | 1.234197 | +0.43% |
| `mcf_13715_0.00884597` | 0.008846 | 42,519,006 | 41,847,020 | -1.58% | 0.940756 | 0.955863 | +1.61% |

## 文件

- `cause_metrics.csv`：用于解释性能变化的关键计数器汇总。
- `counter_comparison.csv`：全部 457,080 条逐切片计数器记录，A=THP off、B=THP on。
- `slice_performance.csv`：对调后的逐切片周期、IPC、主机时间。
- `performance_cycles.svg`、`performance_ipc.svg`：对调标注后的可视化。
