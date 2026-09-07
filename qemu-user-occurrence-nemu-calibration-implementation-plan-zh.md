# QEMU-user 动态 occurrence + NEMU 坐标标定实施方案

## 1. 目标和结论

本方案用于继续完成 `cross-config-slice-position-alignment-roadmap-zh.md` 中尚未闭环的 M2/M3。推荐采用两类执行器分工，而不是尝试把 QEMU-user 指令数换算成 NEMU 指令数：

- QEMU-user TCG plugin 快速采集 A/B 的稀疏语义事件顺序，得到跨构建的 `(anchor, occurrence)` 对应；
- NEMU 负责把源端 SimPoint 坐标绑定到 A 的语义事件，并把目标语义事件物化为 B 的 workload icount、interval/offset 和可选 checkpoint。

最终输出必须是：

```text
(build_id_B, anchor_id_B, occurrence_B, before_instruction,
 workload_icount_B, interval_B, offset_B)
```

QEMU-user 的事件序号只用于排序和对齐，不作为目标 SimPoint 坐标。

## 2. 为什么需要三段标定

```text
ELF A/B + manifest
        |
        +--> anchor catalog + PC watchlist
                         |
                         +--> QEMU-user A/B occurrence trace
                         |                  |
A SimPoint icount -------+--> NEMU A 局部窗口 --唯一定位--> A occurrence
                                                    |
                                             PositionAligner
                                                    |
                                             B occurrence
                                                    |
                                             NEMU B 命中
                                                    |
                                      B icount/interval/checkpoint
```

仅有 QEMU A/B trace 还缺少 `A SimPoint icount -> A occurrence` 这一步。QEMU-user 不执行 guest kernel，不能用它的总指令数直接定位 NEMU 的 A1。优先从现有 A warmup checkpoint 恢复，在 A1 附近采一个很小的 NEMU 语义事件窗口，再把这个窗口唯一匹配到 QEMU A trace；只有没有可用 checkpoint 或局部窗口不唯一时，才回退到 A 从头稀疏执行。

B 端正式验收必须按路线图从 workload 计数起点运行，累计目标 anchor 的 occurrence。这个过程只做 PC 命中和局部计数，不产生完整指令 trace；命中后同时得到正确的 NEMU workload icount，并可直接生成 checkpoint。已有 B checkpoint 可用于冒烟调试，但不能替代 from-scratch 的唯一可达与运行输出验证。

## 3. v1 范围

首版只支持当前实验需要的条件：

- 同一 workload、同一输入、同一功能路径；
- 单线程、确定性执行；
- 静态链接 `ET_EXEC` RISC-V ELF；
- 事件统一为 anchor 入口的 `before_instruction`；
- occurrence 在每个 build、每个 anchor 内从 0 开始；
- BBV/总指令比例只用于选择候选和诊断，不参与语义身份判定。

首版不实现 PIE/load-bias、多线程逻辑时钟、完整指令 trace、QEMU/NEMU icount 换算、DCFG/IR 推断和通用模拟器 adapter。遇到这些输入直接拒绝，不增加兼容层。

## 4. 组件和产物契约

### 4.1 Watchlist 生成器

复用 `experiment/dwarf_source.py` 的 catalog。Python 侧从 catalog 生成每个 build 的紧凑 watchlist：

```text
watch_id<TAB>absolute_pc
```

审计信息单独写入 `watchlist-manifest.json`，至少包含 ELF SHA-256、catalog SHA-256、suite/run manifest SHA-256、`watch_id -> anchor_id/semantic_key/confidence` 映射和 `event_phase`。v1 若同一 PC 对应多个候选 anchor，生成阶段拒绝，避免在插件内实现消歧。

### 4.2 QEMU-user occurrence collector

新增目录建议为 `experiment/qemu_occurrence/`：

- `occurrence_plugin.c`：TCG plugin；
- `collect.py`：生成 watchlist、启动 QEMU、校验运行并规范化 trace；
- `README.md`：记录构建与复现实验命令。

插件在 TB translation callback 中读取 `qemu_plugin_insn_vaddr()`，只给 watchlist PC 注册 `qemu_plugin_register_vcpu_insn_exec_cb()`。动态 callback 只写 `watch_id`，不对其他指令插桩，不统计 QEMU 总 icount。

原始流使用固定 little-endian `uint32 watch_id`，文件顺序即动态事件顺序。Python 离线按 anchor 计数，恢复零基 occurrence 并生成 `OccurrenceTrace`。每条记录只占 4 字节，避免当前 `-d exec,nochain` 约 600 MB/侧的文本日志。

Collector 的外部契约保持为：

```text
collect(run_manifest, catalog, watchlist) -> OccurrenceTrace
```

它隐藏插件构建、QEMU 命令、二进制 trace 解码和 provenance；PositionAligner 不感知 QEMU。

### 4.3 PositionAligner

继续复用 `align_occurrence_sequences()` 和 `project_event()`：

- 输入是 A/B `OccurrenceTrace`；
- 只允许 semantic key/correspondence 一致且 event phase 相同的匹配；
- 保持严格单调、显式 gap 和全局 top-1/top-2 ambiguity 拒绝；
- A/B occurrence 不要求数值相等。

先测量 reference trace 规模。若 `(len(A)+1)*(len(B)+1)` 未超过现有预算，直接复用当前 DP；只有实测超限时，才增加“稀有同步 anchor 分段 + 段内有界 DP”，且每段都必须保留唯一性证据。不要预先实现一套通用大规模对齐框架。

### 4.4 NEMU semantic position locator

扩展已有 `src/checkpoint/semantic_point.cpp`，不新建第二套 checkpoint 状态机。新增一个面向位置的输入（建议 CLI 为 `--semantic-position FILE`），输入使用简单的版本化文本格式，Python 侧另存 JSON provenance。支持两个模式：

1. `trace-window`：从 A warmup checkpoint 恢复，在给定 source icount 周围记录 watchlist 命中及其 NEMU workload icount；
2. `checkpoint-on-occurrence`：B 从 workload 起点运行，对目标 PC 做零基 occurrence 计数，命中指定 occurrence 时记录位置并生成 checkpoint。

NEMU 的关键修改点位于 `per_bb_profile()`：在前一基本块结束、下一基本块 `s->pc` 尚未执行时检查入口 PC。因此命中状态应保存为 `cpu.pc = s->pc`，事件定义为 `before_instruction`。不能沿用当前“基本块执行结束后累计 range”的语义冒充入口事件。

NEMU 输出 `semantic-hit.json`，至少包含：

```json
{
  "watch_id": 17,
  "occurrence": 9181,
  "event_phase": "before_instruction",
  "pc": 113812,
  "workload_icount": 896560123456,
  "interval_size": 20000000,
  "interval": 44828,
  "offset": 123456
}
```

运行时只保留一个小型最近事件 ring buffer。目标命中时把上下文摘要写入结果，与 QEMU B 目标事件附近的上下文比较；不持续输出整条 NEMU trace。

### 4.5 编排和报告

新增一个薄编排脚本，把下面五个现有/新增动作串起来：

```text
build catalog -> collect QEMU A/B -> calibrate NEMU A
              -> project A event to B -> materialize NEMU B
```

编排脚本只传递版本化产物，不重新实现 catalog、序列对齐或 checkpoint。最终报告必须同时绑定 A/B ELF、firmware、run manifest、QEMU/plugin、NEMU、watchlist、trace 和 checkpoint hash。

## 5. 分阶段实施

### P0：冻结首个实验和可执行性检查（0.5-1 天）

首个真实样本选择 `libquantum:A28258->B28259`：它是当前最早的 reference-input 非零位移候选，BBV global 结果为 `matched` 且窗口稳定；在 B 必须 from-scratch 的验收约束下，它的 NEMU 成本最低。冻结：

- argv：`1397 8`；
- interval size：`20,000,000`；
- A source icount：`28258 * 20,000,000`；
- A/B ELF、firmware、运行脚本和工具 hash；
- source warmup checkpoint 的实际起始 icount 及可恢复性。

现有 A checkpoint 已确认存在于：

```text
/nfs-nvme/home/share/checkpoints_profiles/spec06_gcc16_rv64gcb_260724/
  checkpoint/libquantum/28258/_28258_0.006347_memory_.zstd
```

A checkpoint 的生成日志记录 workload icount `565140000001`，距离 A1=`565160000000` 还差 `19,999,999` 条，因此 A 无需从 workload 起点重放。B 正式运行目标约为 `565180000000` 条 workload 指令；现有同次 profiling 日志给出的 NEMU 吞吐约为 2.09 亿 instr/s，只作为排期估算，实际运行仍记录本次耗时。

同时审计 libquantum workload anchor 的 DWARF 来源。当前选中的 workload function 是 symbol-only，因此即使动态闭环成功也只能记为 L；不得在报告阶段升级置信度。

完成条件：输入和功能路径相同；source checkpoint 位于 A1 之前且能恢复；否则记录明确原因并选择同 workload 的下一个可恢复候选。

### P1：QEMU plugin MVP（1-2 天）

1. 实现 watchlist 解析、translation 过滤、动态 callback 和 4-byte event stream；
2. 实现 Python wrapper、trace 完整性检查和 `OccurrenceTrace` 转换；
3. 用现有 `libquantum 20 1` 对照 `-d exec,nochain` 结果；
4. A/B 均要求事件数 95、事件顺序/occurrence 完全一致、stdout hash 相同；
5. 同一侧重复运行一次，要求规范化 trace hash 一致。

完成条件：插件结果与现有 95-event 证据逐事件相同；原始 QEMU 文本 trace 不再生成；非零退出、多 vCPU、非法 watch ID 或截断 stream 均拒绝。

### P2：A 源坐标局部窗口（1-2 天）

1. 扩展 NEMU semantic position locator 和结果输出；
2. 从已确认的 A `28258` checkpoint 恢复，运行约 2000 万条指令到 A1；
3. 在 A1 前后收集有限的语义事件 token 和精确 NEMU workload icount；
4. 记录 A1 是 exact anchor hit，还是到前后 anchor 的有符号 snap delta；
5. 从实际窗口选择少量、有区分度且 A/B catalog 均可对应的 anchor，生成 QEMU watchlist。

完成条件：恢复成功，窗口覆盖 A1，event phase 为 `before_instruction`，并得到可供 QEMU A 全局定位的 token 序列。A1 不在 anchor 上时只记录事实，不在此阶段强行接受 snapped 位置。

### P3：reference-input A/B occurrence trace 和 A 绑定（执行时间另计）

1. 用 `1397 8` 分别执行 QEMU-user A/B，只监控 P2 选出的 anchors；
2. 记录 argv/env、stdout/stderr/exit status、运行时长、trace bytes/events/hash；
3. 在 QEMU A trace 中精确查找 P2 的 NEMU A token 窗口；
4. 唯一命中后得到 `(anchor_A, occurrence_A)`，并按冻结 snap 上限决定接受或 `LOW_FIDELITY`；
5. 用当前 DP 对齐 QEMU A/B；仅在实测触发 `SEARCH_TRUNCATED` 后实现分段；
6. acceptance 前至少重复一侧，确认规范化 occurrence trace 稳定。

完成条件：A/B 功能输出一致；NEMU A 局部上下文在 QEMU A 中唯一；目标候选存在唯一单调对应。失败时以 `INCOMPATIBLE_RUN`、`NO_ANCHOR`、`AMBIGUOUS` 或 `SEARCH_TRUNCATED` 结束。

### P4：B 坐标物化和 checkpoint（1-2 天，加 NEMU 执行时间）

1. PositionAligner 把 A event 投影为唯一 B event；
2. 生成只包含目标 PC/occurrence 和少量上下文 anchor 的 NEMU target 文件；
3. B 从 workload 计数起点运行约 5652 亿条指令，只累计受监控 PC；
4. 命中 B occurrence 时输出 NEMU workload icount、interval/offset 和上下文摘要；
5. 在目标指令执行前生成 B-native checkpoint；
6. 恢复 checkpoint，做 bounded slice 检查，并继续到 `HIT GOOD TRAP` 或等价 terminal marker；
7. 将 from-scratch 的目标前后 marker 序列和功能输出作为正式验收对照。

完成条件：NEMU B occurrence 和 QEMU B 事件一致；上下文一致；checkpoint PC/event phase 正确；恢复后功能输出和 terminal marker 正确。仅有 zero exit 或 checkpoint 文件不算通过。

### P5：证据升级和 held-out（2-4 天，加构建/执行时间）

P0-P4 跑通后，libquantum 参考样本仍是 L 级，因为现有 workload anchor 缺少可追溯 source identity。它证明工程闭环，不满足 M2 的 real H/M gate。

随后单独准备一组保留 workload DWARF 的 A/B 构建；如果确认 LTO 是丢失 source anchor 的原因，最小改动是为验证构建保留 `-g` 并关闭该 workload 的 LTO。该构建只用于 E1/M 级校准，不替换现有 reference artifacts。

- 一个非零位移点作为 calibration；
- 另一个点作为 held-out，冻结 policy 后再运行；
- calibration/held-out 分开统计 exact、snapped、rejected；
- 只有 DWARF/source anchor、动态 occurrence、上下文唯一性和 NEMU runtime 都成立时标为 M。

完成条件：至少一个 real M accepted 和一个 held-out 结果；错误匹配为 0；否则 M2/M3 继续保持 partial。

## 6. 验收矩阵

| 层级 | 必须满足 | 失败处理 |
|---|---|---|
| 运行兼容 | workload/input/argv/env/功能输出一致 | `INCOMPATIBLE_RUN` |
| 产物身份 | ELF/catalog/manifest/tool/watchlist hash 一致 | `ARTIFACT_MISMATCH` |
| QEMU collector | 零基 occurrence、单 vCPU、stream 完整、重复稳定 | `EVIDENCE_COLLECTION_FAILED` |
| A 坐标绑定 | NEMU 局部窗口在 QEMU A 中唯一 | `AMBIGUOUS` / `OUT_OF_TRACE` |
| 跨构建对齐 | 严格单调、top-1 唯一、未超预算 | `AMBIGUOUS` / `SEARCH_TRUNCATED` |
| B 物化 | 目标 occurrence、上下文、before-instruction PC 一致 | `LOW_FIDELITY` |
| checkpoint | B-native、可恢复、terminal marker 正确 | runtime validation failed |
| 置信度 | symbol-only 保持 L；DWARF/source + 动态唯一性才可 M | 不允许人工升级 |

## 7. 源码副本和修改边界

QEMU 和 NEMU 都必须先复制到当前 `checkpoint_align` 目录，再进行任何源码修改、配置或编译。原始 checkout 只用于读取和核对，不得直接写入。

建议目录布局：

```text
checkpoint_align/
  local-src/
    qemu-11.0.0/       # 与 qemu-riscv64 11.0.0 匹配的源码副本
    NEMU/               # NEMU 工作源码副本
  local-build/
    qemu/
    NEMU/
  source-provenance.json
```

P0 在复制前先记录两个原始源码树的绝对路径、`git rev-parse HEAD`、分支、`git status --short`、未提交 diff hash、子模块状态和复制时间。然后使用保留权限、符号链接和时间戳的目录复制（例如 `cp -a SOURCE local-src/NAME`），复制后对关键源文件和目录清单重新计算 hash，写入 `source-provenance.json`。复制必须是完整源码树；不要只复制单个 `.c` 文件或只复制干净 commit，从而丢失当前实验所依赖的本地改动和子模块状态。

从复制完成开始，所有 QEMU 构建、plugin ABI 编译检查和 NEMU 修改都只针对 `local-src/qemu-11.0.0/`、`local-src/NEMU/`；编译输出放在 `local-build/`，不污染源码副本。每次报告绑定副本的 source hash、构建配置和最终二进制 hash。原始源码若发生变化，必须重新复制并生成新的 provenance，不得在同一 run 中混用两个版本。

QEMU 源码路径目前尚未由安装二进制反推出，实施时先找到能重现本机 `qemu-riscv64 version 11.0.0` 的源码；NEMU 默认候选是 `/nfs/home/wujiabin/work/260820_NEMU_paper/NEMU`，但仍需在复制前重新检查其分支和脏状态。

## 8. 实施顺序

建议按下面顺序提交，每一步都能独立复核：

1. P0：复制 QEMU/NEMU 源码并冻结 `source-provenance.json`；
2. 当前实验目录：QEMU plugin、watchlist/trace wrapper、小输入等价证据；
3. `local-src/NEMU/` 副本：扩展现有 semantic point 为入口 occurrence locator；
4. 当前实验目录：A checkpoint 局部窗口匹配和 B target 生成；
5. reference-input 首个 L 级闭环报告；
6. 保留 workload DWARF 的 M 级 calibration/held-out。

当前 `checkpoint_align` 顶层不是 Git 仓库。实施 NEMU 修改前，先在原始 NEMU checkout 中检查 `git status --short --branch` 和 worktree，再复制；复制后不再回写原始 checkout。现查候选仓库 `/nfs/home/wujiabin/work/260820_NEMU_paper/NEMU` 为 `feat-profiling-accel`，且已有 `semantic_point.cpp`。由于目标是使用本目录中的副本，后续不因原仓库脏状态创建额外 worktree；只有需要比较另一份基线时才另行隔离。

验证以一次性运行证据为主。只有二进制 trace 解码、零基 occurrence 或 before-instruction 边界这类容易在后续改动中真实回归、且已有合适测试入口的行为，才提交可复用测试。

## 9. 主要风险与控制

- **QEMU-user 与 full-system 路径不同**：QEMU 只建立 application anchor 顺序；A/B NEMU 标定和上下文一致性负责最终坐标证明。
- **reference trace 太大**：先从 NEMU A 局部窗口选择相关 anchors，插件只写 4-byte watch ID，并对事件数/字节数设置显式预算；先测量，再决定是否做分段对齐。
- **source checkpoint 不含 occurrence counter**：用 A checkpoint 恢复后的语义子序列回填其在 QEMU A 全局 trace 中的位置，不修改旧 checkpoint 格式；任何非唯一绑定都拒绝 A 坐标。B 正式运行仍从零累计。
- **目标 anchor 过密**：只在目标 B 运行中计数，不输出每次命中；用固定大小 ring buffer 留上下文。
- **现有 anchor 只有 L**：先把它作为工程闭环，M 级证据使用单独的可追溯 DWARF 构建，不混淆两个结论。
- **触发边界偏一条指令**：NEMU 在进入 `s->pc` 前检查并保存状态，小输入 identity run 对照 PC、icount 和恢复后的第一条指令。
- **误改原始源码**：编译和修改命令统一以 `local-src/` 为根；运行前检查 `source-provenance.json` 和原始 checkout 状态，发现路径指向原目录立即拒绝。
- **源码副本不完整或版本漂移**：复制时保留子模块/未提交 diff，副本和二进制都写入 hash；QEMU plugin ABI 与 NEMU 结果不得跨 provenance 混用。

## 10. 预计产物

```text
experiment/qemu_occurrence/
  occurrence_plugin.c
  collect.py
  README.md
  results/<run>/{watchlist.tsv,watchlist-manifest.json,trace.u32,occurrences.json,run-manifest.json}

local-src/
  qemu-11.0.0/
  NEMU/
local-build/
  qemu/
  NEMU/
source-provenance.json

experiment/m3/runtime/<candidate>/
  source-window.json
  source-binding.json
  sequence-alignment.json
  target-position.json
  semantic-hit.json
  checkpoint-manifest.json
  validation-summary.json
```

首个交付点是 P1 的 95-event 等价结果；第一个端到端交付点是 `libquantum:A28258->B28259` 的 L 级 runtime-validated 报告；M2/M3 的完成点是后续 M 级 calibration/held-out gate，而不是第一个 L 级 checkpoint。
