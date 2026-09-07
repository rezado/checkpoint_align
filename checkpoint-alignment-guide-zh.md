# Checkpoint Align 代码、使用方法与当前结果

本文档说明本仓库的代码结构、输入约定、完整使用流程、输出文件和当前已有结果。结果快照基于 2026-09-07 当前工作区中的机器可读 JSON；运行产物默认被 `.gitignore` 排除，不随源码提交。

## 1. 项目目标

本项目用于把同一 workload 在两个不同构建配置下的 SimPoint 位置进行对应，并在目标构建上生成新的原生 checkpoint。典型场景是：

- source 构建已经具有 SimPoint、BBV profile 和 checkpoint；
- target 构建运行相同 workload 和输入，但 ELF、指令布局或 ISA 配置不同；
- 不能直接把 source 的 PC、基本块编号或指令数位置照搬到 target；
- 需要先找到 target 中的候选 interval，再生成 target-native checkpoint，并通过 restore 后的动态行为进行有界验证。

这里的“对齐”只处理 SimPoint 位置，不使用 SimPoint 权重判断位置。`weights0` 不参与 BBV 匹配得分；checkpoint 生成时写入的单点权重 `1.0` 只用于满足 NEMU checkpoint 输入格式。

## 2. 代码结构

### 2.1 通用工作流入口

[`experiment/cross_elf_checkpoint.py`](experiment/cross_elf_checkpoint.py) 是日常使用的主入口，提供四个子命令：

| 子命令 | 作用 | 是否运行 NEMU |
|---|---|---:|
| `prepare` | 发现或选择 workload，将两侧 profile 输入复制到一个自包含 suite | 否 |
| `align` | 把 source SimPoint 映射到 target BBV interval 候选 | 否 |
| `checkpoint` | 生成一个 target-native checkpoint，并对 source/target checkpoint 做有界 restore/profile | 是 |
| `report` | 汇总 suite 中已有的 alignment 和 checkpoint 结果 | 否 |

主入口采用 manifest 驱动，不把 workload 名称和构建标签写死在代码中：

- 未指定 `--workloads` 时，自动发现两侧共有并且输入完整的 workload；
- 默认侧标签为 `A` 和 `B`，也可设置为 `baseline`、`candidate` 等任意不同名称；
- 后续命令从 `suite-manifest.json` 读取 workload、interval 和侧标签；
- 新生成的 manifest 使用 suite 相对路径，因此整个 suite 可以移动；
- 旧 manifest 没有 `sides` 时仍按 `A/B` 读取。

### 2.2 BBV PositionAligner

[`experiment/position_aligner/bbv.py`](experiment/position_aligner/bbv.py) 实现独立的 BBV PositionAligner：

- 每个 BBV 文件只建立一次索引；
- 以总指令数比例估计 target 搜索中心；
- 综合局部 BBV、相邻序列上下文和进度一致性打分；
- 自适应扩大候选窗口；
- 使用稀疏动态规划选择全局单调路径；
- 比较 top-2 路径 margin；
- 输出 `AMBIGUOUS`、`SEARCH_TRUNCATED`、`INCOMPATIBLE_RUN` 等类型化拒绝原因；
- 绑定 manifest、ELF hash 和运行身份；
- 不读取 `weights0`，也不直接生成 checkpoint。

[`experiment/position_aligner/__main__.py`](experiment/position_aligner/__main__.py) 提供 prepared suite 的 replay 命令。

需要注意，主入口 `cross_elf_checkpoint.py align` 当前使用较直接的“比例中心 + BBV count-multiset overlap + 邻域上下文 + 顺序过滤”方法；`position_aligner` 是更严格的稀疏全局方法。两者的接受数量不能直接混用。

### 2.3 语义锚点和动态 occurrence

[`experiment/dwarf_source.py`](experiment/dwarf_source.py) 用于：

- 从 ELF 的 DWARF、符号表和反汇编中建立 source/function/inline/loop anchor catalog；
- 将 PC trace 转换为带 occurrence 编号的语义事件；
- 对两侧 occurrence 序列做全局或分段单调对齐；
- 根据锚点质量标记 `M/L/R` confidence；
- 将语义事件投影到 target workload 指令位置。

[`experiment/qemu_occurrence/collect.py`](experiment/qemu_occurrence/collect.py) 和 `occurrence_plugin.c` 用 QEMU plugin 收集指定 PC/anchor 的动态出现序列。

`experiment/m0` 到 `experiment/m3` 保存协议 fixture、受控验证、真实 workload trace 和校准脚本。这些是算法演进与证据，不是最简日常入口。

## 3. 输入目录约定

source 和 target profile root 默认采用下面的导出布局：

```text
<profile-root>/
├── elf/<workload>.elf
├── bin/<workload>.fw_payload.bin
├── cmd/<workload>.run.sh                         # 可选
├── json/<workload>.json
├── cluster/<workload>/simpoints0
├── cluster/<workload>/weights0                  # 可选，不参与位置匹配
├── profiling/<workload>/simpoint_bbv.gz
├── logs/profiling/<workload>/profiling.out.log  # 可选
├── logs/build_elf/<workload>.log                # 可选
└── checkpoint/<workload>/<point>/*_memory_.zstd # checkpoint 阶段需要 source 侧存在
```

单 workload 独立目录也可以使用以下扁平形式：

```text
cluster/simpoints0
cluster/weights0
profiling/simpoint_bbv.gz
logs/profiling.out.log
logs/build.log
```

自动发现以两侧 `json/*.json` 的共同文件名为候选，并要求两侧同时具备 ELF、firmware、JSON、SimPoint 和 BBV。JSON 至少需要包含：

```json
{
  "lbm": {
    "insts": 873620000017
  }
}
```

两侧必须对应同一 workload、同一功能输入和可比较的 workload-relative instruction domain。代码不会把不同输入造成的执行差异自动修正成“对齐”。

## 4. 快速使用

以下命令均从仓库根目录执行。

### 4.1 准备全部共有 workload

```sh
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite /path/to/alignment-suite \
  --source-root /path/to/source-profile \
  --target-root /path/to/target-profile \
  --nemu /path/to/riscv64-nemu-interpreter \
  --gcpt /path/to/gcpt.bin
```

不传 `--workloads` 时会自动选择两侧共有且必要文件完整的 workload。只处理部分 workload 时使用：

```sh
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite /path/to/alignment-suite \
  --source-root /path/to/source-profile \
  --target-root /path/to/target-profile \
  --workloads lbm mcf cactusADM \
  --nemu /path/to/riscv64-nemu-interpreter \
  --gcpt /path/to/gcpt.bin
```

只运行 `prepare` 和 `align` 时可以暂不提供 NEMU/gcpt；执行 `checkpoint` 前必须保证 suite 的 `tools/` 下有对应文件。

### 4.2 使用自定义构建标签

```sh
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite /path/to/alignment-suite \
  --source-root /path/to/baseline-profile \
  --target-root /path/to/candidate-profile \
  --source-side baseline \
  --target-side candidate
```

生成后的目录会使用：

```text
workloads/<workload>/baseline/
workloads/<workload>/candidate/
```

后续命令无需重复传标签。

### 4.3 对齐 SimPoint 位置

对 manifest 中的全部 workload 扫描：

```sh
python3 experiment/cross_elf_checkpoint.py align \
  --suite /path/to/alignment-suite
```

只扫描指定 workload：

```sh
python3 experiment/cross_elf_checkpoint.py align \
  --suite /path/to/alignment-suite \
  --workloads lbm mcf
```

只检查 source 的特定 SimPoint：

```sh
python3 experiment/cross_elf_checkpoint.py align \
  --suite /path/to/alignment-suite \
  --workloads lbm \
  --points 20 22
```

主要参数及默认值：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--radius` | 8 | 比例中心左右搜索的 target interval 数 |
| `--context` | 2 | 参与上下文得分的前后 BBV interval 数 |
| `--min-score` | 0.55 | 候选最低综合得分 |
| `--min-margin` | 0.10 | 最优和次优候选的最低差值 |

`--points` 会覆盖该次 workload 的 `alignment.json`。做临时单点诊断后，如果希望恢复完整报告，需要再次执行不带 `--points` 的完整 align。

### 4.4 生成并验证 target checkpoint

```sh
python3 experiment/cross_elf_checkpoint.py checkpoint \
  --suite /path/to/alignment-suite \
  --workload lbm \
  --source-point 20
```

该命令依次执行：

1. 从 `alignment.json` 找到 source point 对应的 target point；
2. 默认只接受 `accepted_experimental` 候选；
3. 从原 source profile root 复制 source checkpoint；
4. 用 target firmware 和 target point 生成 target-native Zstandard checkpoint；
5. 运行 `zstd -t` 检查 target checkpoint；
6. 分别 restore source 和 target checkpoint；
7. 各自继续采集两个 20M-instruction BBV window；
8. 比较 restore 后窗口，并写入 `checkpoint-result.json`。

主要参数及默认值：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--warmup` | 20,000,000 | checkpoint 前 warmup 指令数 |
| `--boot-allowance` | 180,000,000 | firmware/启动阶段的指令预算 |
| `--generation-tail` | 20,000,000 | trigger 后附加执行预算 |
| `--restore-instructions` | 40,000,000 | restore 后继续执行的指令数 |
| `--min-post-restore-overlap` | 0.5 | 每个比较窗口的最低 overlap |
| `--timeout` | 180 秒 | 单次 NEMU 命令超时 |
| `--force` | 关闭 | 允许仅为诊断而使用被拒绝候选 |

`--force` 不会提高 confidence，也不会把结果变为 production eligible。

### 4.5 生成汇总报告

```sh
python3 experiment/cross_elf_checkpoint.py report \
  --suite /path/to/alignment-suite
```

### 4.6 使用全局 PositionAligner replay

```sh
python3 -m experiment.position_aligner replay \
  --suite /path/to/alignment-suite \
  --output /tmp/position-aligner-report.json
```

可使用 `--workloads` 限定 workload，并通过 `--max-window`、`--min-score`、`--min-global-margin`、`--max-seconds` 和 `--max-memory-bytes` 控制搜索和资源预算。

## 5. Suite 和输出文件

```text
<suite>/
├── suite-manifest.json
├── tools/
│   ├── riscv64-nemu-interpreter
│   └── gcpt.bin
├── workloads/<workload>/<side>/
│   ├── elf/
│   ├── bin/
│   ├── cmd/
│   ├── json/
│   ├── cluster/
│   ├── profiling/
│   ├── logs/
│   └── checkpoint/            # source checkpoint 被复制后出现
└── results/
    ├── alignment-summary.json
    ├── suite-report.json
    └── <workload>/
        ├── alignment.json
        └── <source-side><source-point>-<target-side><target-point>/
            ├── checkpoint-result.json
            ├── target-cluster/
            ├── target-generation/
            ├── source-restore/
            ├── target-restore/
            └── logs/
```

重要状态字段：

| 状态 | 含义 |
|---|---|
| `accepted_experimental` | BBV score、margin 和单调性通过当前阈值，可进入 checkpoint 实验 |
| `rejected_ambiguous` | 最优候选得分不足或与次优候选差距不足 |
| `rejected_non_monotonic` | 接受候选会破坏 source 到 target 的顺序关系 |
| `validated_experimental` | checkpoint 生成、结构检查、两侧有界 restore 和 BBV overlap 门槛通过 |
| `rejected_post_restore_divergence` | checkpoint 可运行，但 restore 后至少一个 BBV 窗口低于阈值 |

`validated_experimental` 不是 workload 完整运行成功，也不是源代码语义等价证明。结果中仍保留：

```json
{
  "bounded_restore_only": true,
  "workload_terminal_completion": false,
  "production_eligible": false
}
```

## 6. 当前已有结果

### 6.1 当前数据集

当前 prepared suite 为 `experiment/multi-workload`：

- source：`spec06_gcc16_rv64gcb_260724`
- target：`spec06_gcc16_rva23_novec_260726`
- interval：20,000,000 workload instructions
- prepared workload：`lbm`、`mcf`、`astar_biglakes`、`libquantum`、`bwaves`、`cactusADM`
- 在当前两侧完整 profile root 上，自动发现逻辑可识别 55 个共有且必要文件完整的 workload；当前 suite 只准备了其中 6 个。

### 6.2 通用入口的六 workload BBV 扫描

当前完整 `cross_elf_checkpoint.py align` 结果：

| Workload | Source SimPoint | 接受 | 拒绝 | 最早接受的 target point | 最高接受分数 |
|---|---:|---:|---:|---:|---:|
| `lbm` | 24 | 9 | 15 | 20 | 0.9323 |
| `mcf` | 22 | 1 | 21 | 1 | 0.8303 |
| `astar_biglakes` | 46 | 2 | 44 | 5 | 0.9031 |
| `libquantum` | 52 | 12 | 40 | 17,927 | 0.7119 |
| `bwaves` | 49 | 16 | 33 | 2,003 | 0.9292 |
| `cactusADM` | 16 | 10 | 6 | 7 | 0.9640 |
| **总计** | **209** | **50** | **159** | — | — |

这些是未标注真实对应关系上的候选筛选结果，50/209 不是 accuracy，也不是对齐覆盖率目标。

### 6.3 已执行的 checkpoint 对

4 个候选已经实际生成 target checkpoint，并对 source/target 各执行 40M 指令的有界 restore/profile：

| Pair | Alignment score | Margin | 生成日志中的 checkpoint instruction | Restore BBV overlap | 当前状态 |
|---|---:|---:|---:|---|---|
| `lbm A20 -> B20` | 0.9323 | 0.8852 | 380,000,000 | 0.7986, 0.9846 | `validated_experimental` |
| `mcf A1 -> B1` | 0.8303 | 0.4930 | 9 | 0.9561, 0.9144 | `validated_experimental` |
| `astar_biglakes A5 -> B5` | 0.9031 | 0.1384 | 80,000,001 | 0.9831, 0.9463 | `validated_experimental` |
| `cactusADM A7 -> B7` | 0.8682 | 0.4151 | 120,000,032 | 1.0000, 0.9454 | `validated_experimental` |

四组结果均满足：

- target checkpoint 已生成并通过 Zstandard 完整性检查；
- source 和 target restore 进程返回 0；
- 两侧日志均出现 NEMU good state；
- 两个 post-restore BBV window 均高于默认 0.5 门槛。

`mcf` 数据是 2026-09-07 重新运行后的当前值；其 target checkpoint SHA-256 为 `48d7476d9c10fdefe110e61998347e21154a0b0f7e4920b38c84590c4c9374f6`。生成日志记录 checkpoint instruction 为 9，本文只记录工具实际输出，不把该数字解释为 source/target 语义位置相同。

### 6.4 全局 PositionAligner M1 replay

独立的稀疏全局 BBV replay 同样处理 209 个 source point：

| Workload | Source point | `matched` | `AMBIGUOUS` rejected |
|---|---:|---:|---:|
| `lbm` | 24 | 4 | 20 |
| `mcf` | 22 | 6 | 16 |
| `astar_biglakes` | 46 | 36 | 10 |
| `libquantum` | 52 | 6 | 46 |
| `bwaves` | 49 | 6 | 43 |
| `cactusADM` | 16 | 4 | 12 |
| **总计** | **209** | **62** | **147** |

由于每个 workload 都仍包含被拒绝位置，workload 级总体状态为 `rejected/AMBIGUOUS`。位置级的 62 个 match 仍只有 confidence `L`，`production_eligible=false`。

### 6.5 DWARF/occurrence 阶段

当前静态 catalog 已覆盖：

- 6 个 workload；
- 12 个 A/B ELF；
- 12 个 build 都已绑定 suite manifest；
- 所有 catalog 状态为 `ok`。

M2 汇总状态为 `partial`：

- 受控 native O0/O2 插入实验：5/5 已标注事件正确匹配，1 个负例正确拒绝，confidence `M`；
- `libquantum-small` 真实动态实验：功能输出一致，95 个事件得到 exact position，对齐通过；但所选函数在现有 ELF 中只有 symbol anchor，因此 confidence 仍为 `L`；
- held-out 的真实 H/M reference-input 对仍未完成；
- 8 个 shifted reference candidate 在该阶段仍是 `candidate_only`，没有独立 runtime trace 验证；
- 总体 `production_eligible=false`，且 `weights_used=false`。

M3 的 debug-preserving validation build 已得到一个限定范围内的 `M` 级结果：

- source anchor：`quantum_sigma_x`，`gates.c:156`；
- calibration occurrence 0 和 held-out occurrence 1000 都分类为 exact；
- wrong match 为 0；
- checkpoint 通过 `zstd -t`；
- restore 最终到达 `HIT GOOD TRAP`。

但该结果只适用于专门构建的 `debug-preserving-validation-builds-only`，并不替换现有 reference profile artifacts。reference candidate `libquantum:A28258->B28259` 虽然已有独立 runtime validation，仍因 symbol-only anchor 保持 confidence `L` 和 `production_eligible=false`。

## 7. 结果边界和下一步

当前已经证明的是：

- 工作流可以处理 manifest 中任意数量和名称的 workload；
- 可以在不同构建标签之间发现、准备和对齐位置；
- target-native checkpoint 的生成、压缩校验和双侧有界 restore 流程可执行；
- 当前四组 checkpoint pair 的短窗口 BBV 行为达到设定门槛；
- suite 可以移动，replay 不依赖准备时的绝对复制路径。

当前尚未普遍证明的是：

- 所有自动发现的 55 个 workload 都已经完成对齐和 checkpoint 生成；
- BBV 候选就是 source/IR 级的同一动态 occurrence；
- restore 后任意长度 ROI 都保持语义和性能等价；
- workload 已运行到最终 trap；
- 当前阈值可以作为未知 workload 的生产准入标准。

要把 reference workload 结果提升到 H/M 级，需要为真实 A/B profile 收集完整、独立、带 workload icount 的 source/IR marker occurrence trace，并在未参与候选选择的 held-out shifted pair 上验证。BBV 和总指令数比例应继续只用于候选召回，不能作为最终语义证据。

## 8. 验证和 Git 管理

运行当前快速验证：

```sh
python3 -m py_compile \
  experiment/cross_elf_checkpoint.py \
  experiment/position_aligner/__main__.py

python3 -m unittest \
  experiment.position_aligner.test_bbv \
  experiment.test_dwarf_source \
  experiment.m3.test_review_shifted_candidates
```

当前结果为 17 个测试通过。

仓库使用本地 `main` 分支管理源码。以下内容被 `.gitignore` 排除：

- copied workload、ELF、firmware 和 emulator；
- BBV/profile、checkpoint 和压缩产物；
- runtime/results/log；
- suite manifest 和派生 report；
- Python cache 和本地构建目录。

因此 Git 中保存的是可复现的实现、测试、fixture 和文档，而大体积运行产物保留在本地 suite 中。
