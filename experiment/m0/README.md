# M0 协议、重复性和真值基线

本目录是 roadmap 的 M0 交付物。它冻结位置协议，保存 synthetic gold，
并从已经存在的 `experiment/lbm` 和 `experiment/multi-workload` 日志/产物
派生 `lbm A20` 重复性证据。不会启动 NEMU，不会改写既有 checkpoint、profile
或 workload 文件；派生脚本只写本目录的 JSON 报告。

## 可复核入口

```sh
python3 experiment/m0/derive_lbm_a20_evidence.py
python3 experiment/m0/validate_m0_fixtures.py
python3 -m json.tool experiment/m0/schema_fixture.json >/dev/null
python3 -m json.tool experiment/m0/gold-correspondence.json >/dev/null
```

`derive_lbm_a20_evidence.py` 的输出是
`experiment/m0/lbm-a20-repeatability.json`；它重新计算日志字段、checkpoint
和 profile SHA-256、两窗口 multiset overlap，以及 legacy/generic 的字节等价性。
每个 run 的 `log_artifacts` 同时保存 generation/restore stdout、stderr 的路径和
SHA；两次 `reproduce.sh` 的 restore 只有 BBV-free 40M 日志，因此不会伪造
post-restore profile SHA。
`validate_m0_fixtures.py` 只依赖 Python 标准库，并检查四个接口的必填字段、
零基 interval 算术、显式 event phase、typed reject、occurrence transform、
identity exact 和边界扰动标签。

## 冻结的最小协议

- `icount_domain` 固定为 `workload_relative_instructions`；不把 boot 或宿主
  指令混入 workload 坐标。
- interval 从 0 开始，`interval = floor(workload_icount / L)`，
  `offset_in_interval = workload_icount % L`。
- fixture 的 event phase 固定写出 `before_instruction`；schema 同时允许
  `after_instruction` 和 `after_taken_edge`，但不能省略 phase。
- 位置身份是 `(build_id, anchor_id, occurrence, event_phase)`。A/B 的
  occurrence 数值不要求相等；插入/删除通过 `occurrence_transform` 表达。
- `matched` 必须带 target；`rejected` 必须带 typed `reason` 且不能强行带
  target。批次协议错误使用 `INCOMPATIBLE_RUN` 等 batch reason，不伪造位置结果。
- `anchor_confidence`、`position_fidelity` 和 `validation_state` 是三个独立
  维度。结果 schema 没有 scoring weight 字段，也不承诺 SimPoint 权重代表性。

`schema_fixture.json` 是四个接口的可执行形状和最小实例：
`BuildRun`、`SourcePosition`、`PositionCorrespondence`、`AlignmentResult`。

## Synthetic gold

`gold-correspondence.json` 明确标记 `synthetic: true`，不冒充真实 workload
准确率。当前包含：

| 类别 | 数量/内容 | 目的 |
|---|---|---|
| identity | A->A 重跑，3 个 exact 边界 | 检查零基、occurrence 和 phase 的 off-by-one |
| controlled positive | target 插入一个 loop event，4 个 matched 位置 | 验证非零 occurrence transform（A 1 -> B 2） |
| controlled snapped | 1 个带 `snap_delta_instructions=-5` 的位置 | 单独记录吸附位移，不伪装 exact |
| position negative | 缺失锚点 `NO_ANCHOR`、重复 phase `AMBIGUOUS` | 证据不足时拒绝 |
| protocol negative | 输入、workload、functional path 不兼容 | 批次返回 `INCOMPATIBLE_RUN`，不生成位置级结果 |
| boundary perturbation | `+1` 和 `-1` 各 1 个，显式标为 matched | occurrence +/-1 是带标签的邻界扰动，不能预设为负例 |

校验器当前报告 `identity_exact=3`、`controlled_positive=4`、
`negative_rejected=5`、`boundary_evaluated=2`。这些是 fixture 完整性计数，
不是算法在真实 workload 上的 precision/coverage。

## `lbm A20` 已有运行证据

以下数值均由派生 JSON 从保存日志和字节重新计算。`profile base` 是日志中的
profiling 起点；`trigger` 是日志同时出现的 `Should take cpt now` /
`Taking checkpoint @` 值；`guest total` 是 NEMU 总 guest icount。四次生成都
有 `Checkpoint done!` 和 `NEMU exit with good state`，但没有 `HIT GOOD TRAP`。

| run | driver/family | profile base | trigger | saved PC | guest total | target checkpoint SHA-256 |
|---|---|---:|---:|---|---:|---|
| `legacy-rerun-20260828-125428` | `experiment/lbm/reproduce.sh` | 119,555,201 | 380,000,005 | `0x1312c` | 540,000,432 | `e8922837ff7ca6bb030249eaeec42d2f45784c82a17e1a2d2174095f26720fed` |
| `legacy-rerun-20260828-125533` | `experiment/lbm/reproduce.sh` | 119,548,301 | 380,000,000 | `0x13134` | 540,000,452 | `cab5de4c5279fc7229f1d5ca02e0dfd46711332d9f8b0a5244e1efa22c17e28a` |
| `legacy-final` | 手工 legacy 命令 | 119,541,067 | 380,000,002 | `0x13134` | 540,000,460 | `f8897ac0b9fcabe3d4fb8be2ce953eb7404256b2bf0a31262d8e9099f8a64272` |
| `generic-suite-lbm-A20-B20` | `cross_elf_checkpoint.py checkpoint` | 119,460,626 | 380,000,000 | `0x11ac6` | 580,000,485 | `858a4102689fbfd9fab20091893e7751ccb92d1188a255559479f0df3ad75ad3` |

四次 workload 命令和输入 md5 都相同：
`./lbm 3000 reference.dat 0 0 100_100_130_ldc.of`，`lbm` md5 为
`1157e82d935a87bd7e8a4b44899ad1c3`。目标 B ELF、firmware、run script、
JSON、simpoints、weights、A source checkpoint，以及 NEMU/gcpt 副本在 legacy
和 generic 目录之间逐项 byte-identical；例如 B ELF SHA 为
`fd853548c6df9ce24110fa1dc050bcb8a10a8deae7e87f08b65fbf1dff0bab45`，
firmware SHA 为 `78e261b68c4c715698507bce1f6b615cd8d94ccb9b6636cdcabc41488c5bbab3`，
NEMU SHA 为 `bbfb15b23d19e34895acab1f61e864dd9f24951f2425357090d854882fde0a42`。

### 关键 legacy/generic 对照

`legacy-final` 的 source/target post-restore profile SHA 分别为
`20ae6192c45cdbf40c58e6888ff3625ad8dd55eda3da84d58b21f81df29db136` 和
`31a35aab6c86c61dabd23bab96efc0925426e817fdccd76300873ea71d4c67f4`；
generic 的 source SHA 相同，target SHA 为
`74763bf9773429adaf79204fb5d25887903b805ce88622391cbee7035ebfaefb`。
两窗口 count-multiset overlap 为：

| run | window 0 | window 1 |
|---|---:|---:|
| legacy-final | 0.7959542656112577 | 0.015444015444015444 |
| generic-suite-lbm-A20-B20 | 0.7985927880386984 | 0.9845559845559846 |

因此重复性 gate 是 `not_met`：同一输入和同一 20M interval 仍出现不同
trigger、PC、checkpoint 和 target profile。这个结果不能用于阈值校准。

## 完整复现实验命令

### Legacy 手工生成

```sh
/usr/bin/time -f 'wall=%e exit=%x' timeout 120s stdbuf -oL -eL \
  /nfs/home/wujiabin/work/260820_NEMU_paper/NEMU/build/riscv64-nemu-interpreter \
  experiment/lbm/B/bin/lbm.fw_payload.bin \
  -D experiment/lbm/generated-B-point20-final -w lbm -C aligned-point20 \
  -b -I 540000000 -S experiment/lbm/B-aligned-cluster \
  --cpt-interval 20000000 --warmup-interval 20000000 \
  --checkpoint-format zstd \
  >experiment/lbm/restore-logs/B-point20-generate-final.out.log \
  2>experiment/lbm/restore-logs/B-point20-generate-final.err.log
```

Legacy bounded restore/profile 使用同一个 `gcpt.bin`，A/B 各自从原始或新
checkpoint restore 40M：

```sh
timeout 60s stdbuf -oL -eL experiment/lbm/tools/riscv64-nemu-interpreter \
  -b -I 40000000 --cpt-restorer experiment/lbm/tools/gcpt.bin \
  --simpoint-profile --dont-skip-boot --cpt-interval 20000000 \
  -D experiment/lbm/post-restore-profile-A -w lbm -C post-restore \
  experiment/lbm/A/checkpoint/20/_20_0.000069_memory_.zstd

timeout 60s stdbuf -oL -eL experiment/lbm/tools/riscv64-nemu-interpreter \
  -b -I 40000000 --cpt-restorer experiment/lbm/tools/gcpt.bin \
  --simpoint-profile --dont-skip-boot --cpt-interval 20000000 \
  -D experiment/lbm/post-restore-profile-B -w lbm -C post-restore \
  experiment/lbm/generated-B-point20-final/aligned-point20/lbm/20/\
_20_1.000000_memory_.zstd
```

两次 legacy `reproduce.sh` 重跑的 driver 命令均为：

```sh
./experiment/lbm/reproduce.sh
```

脚本中的生成参数与上面的 legacy 命令相同，只使用不同的动态输出目录。

### Generic suite

外层命令和重定向：

```sh
python3 experiment/cross_elf_checkpoint.py checkpoint \
  --suite experiment/multi-workload --workload lbm --source-point 20 --timeout 240 \
  > experiment/multi-workload/results/lbm/checkpoint-command.out.log \
  2> experiment/multi-workload/results/lbm/checkpoint-command.err.log
```

该 wrapper 实际执行的 B 生成命令为（由脚本参数和日志共同核对）：

```sh
/nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/tools/riscv64-nemu-interpreter \
  /nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/workloads/lbm/B/bin/lbm.fw_payload.bin \
  -D /nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/lbm/A20-B20/target-generation \
  -w lbm -C aligned -b -I 580000000 \
  -S /nfs/home/wujiabin/work/checkpoint_align/experiment/multi-workload/results/lbm/A20-B20/target-cluster \
  --cpt-interval 20000000 --warmup-interval 20000000 --checkpoint-format zstd
```

Generic source/target restore 都是 `-b -I 40000000`、同一 copied `gcpt.bin`、
`--simpoint-profile --dont-skip-boot --cpt-interval 20000000`，输出目录分别为
`A20-B20/source-restore/profile` 和 `A20-B20/target-restore/profile`；完整
参数和 checkpoint 路径保存在派生 JSON 的 `restore_commands` 字段。

## 差异解释的边界

相关 NEMU 源文件
`/nfs/home/wujiabin/work/260820_NEMU_paper/NEMU/src/monitor/fdt_rng_seed.c`
（SHA-256 `88c53537bf4566c818ae8d249dfe201630670c613958557ffd2fcbe067abeb15`）
在对应日志行实现：当 DTB 没有 `rng-seed` 时，从 `/dev/urandom` 读取 32 字节，
再写入 `/chosen/rng-seed`。四次生成日志都出现
`Patched FDT /chosen with rng-seed`，但日志没有保存实际 seed 字节。因此，
未记录的 host entropy 是首要假设，并且与非重复输出相关；**不能据此证明它是
唯一原因**。

还存在已确认的运行差异：legacy 生成上限是 `-I 540000000`，generic wrapper
因 `boot_allowance + trigger + generation_tail` 使用 `-I 580000000`；路径和
driver wrapper 也不同。这会影响 trigger 之后的停止位置，不能被描述成同一
完整命令的确定性重跑。当前证据只证明 bounded restore 可执行，不证明 marker
语义、ROI 完整性或 workload terminal completion；`HIT GOOD TRAP` 未出现，
所以 `production_eligible` 必须保持 false。

## 当前状态

M0 fixtures 和运行证据已经足够作为后续 M1/M2 的协议输入，但 `lbm A20` 的
非确定性样本必须隔离，不能进入 calibration/held-out accuracy。真正的 A->A
self-map 和 controlled marker 运行仍应由后续 collector 以相同 schema 采集；
本目录的 gold 是可复用的 synthetic contract fixture，不是对既有 BBV score
的语义背书。
