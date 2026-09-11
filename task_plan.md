# Task Plan: General workload checkpoint alignment and local Git setup

## Goal
将当前 checkpoint 对齐入口改为 manifest 驱动，支持不同 workload 和构建侧标签，并在当前目录建立本地 Git 仓库管理代码。

## Phases
- [complete] 梳理现有入口和数据布局，确定兼容性边界
- [complete] 实现自动发现 workload、可配置 source/target 标签和通用结果路径
- [complete] 更新文档与忽略规则，初始化本地 Git 并提交可管理源码
- [complete] 运行核心 CLI/单元验证并记录结果
- [complete] 实际运行代表性 workload checkpoint，并验证 suite relocation 后 replay 仍可用
- [complete] 编写代码、使用方法与当前结果的统一中文文档
- [complete] 将 checkpoint 流程扩展为覆盖 A 侧全部实际 checkpoint 的批处理
- [complete] 为每个 A checkpoint 输出 B 候选、置信度和批量执行状态，并支持断点续跑
- [complete] 用 mcf 的 22 个实际 checkpoint 验证覆盖关系和代表性批处理路径
- [complete] 更新使用文档并提交 Git
- [completed] 根据 checkpoint-all 实际结果导出新的 B slice 集合
- [completed] 输出标准 checkpoint/cluster 布局、slice manifest 与完整性绑定
- [completed] 分别验证仅通过项和包含拒绝项的 mcf 导出
- [completed] 更新文档并提交 Git
- [complete] P0：审计现有实现，冻结动态进度协议、gold fixtures 与 schema 校验
- [complete] P1：打通 DWARF watchlist 与 QEMU 稀疏 occurrence 事件采集/运行清单
- [complete] P2：实现 A source position 的唯一动态窗口绑定与拒绝路径
- [complete] P3：实现带 gap、warp、严格单调和 top-2 判定的跨 ELF 全局序列对齐
- [complete] P4：实现 B semantic target/checkpoint sidecar 物化接口及可用的 NEMU 集成边界
- [complete] P5：实现 restore、cross-build、coverage 三类独立验证与薄 CLI/report
- [complete] 运行单元/集成/CLI 验证，复核最小充分 diff 与剩余端到端证据缺口
- [completed] 定位现有切片目录并确认 checkpoints.json schema
- [completed] 扩展导出命令，分别生成 A/B 切片目录和 checkpoints.json
- [completed] 实际导出 mcf A/B 切片并检查链接、JSON、hash 与压缩完整性
- [completed] 更新文档并提交 Git

## Errors Encountered
| Error | Attempt | Resolution |
|---|---:|---|
| 当前目录不是 Git 仓库 | 1 | 本任务明确要求建立本地仓库，完成代码梳理后执行 git init |
| `unittest discover -s experiment` 无法导入相对模块 | 1 | 改用 `python -m unittest experiment.position_aligner.test_bbv experiment.test_dwarf_source` |
| 在整个 work/share NFS 树查找 `checkpoints.json` 超时 | 1 | 改为已知仓库和共享根目录的定向浅层搜索 |
| 重复创建 `/goal` 失败 | 1 | 当前线程已由用户的 `/goal` 建立 active goal，直接复用 |
| `python3 -m unittest discover -v` 未发现测试 | 1 | 仓库测试位于 namespace package 下，改为显式模块列表运行 |
| 新增内部 `align.py` 覆盖包属性 `align`，6 个 BBV 回归失败 | 1 | 包顶层显式区分动态 `PositionAligner` 与旧 `bbv_align`，恢复兼容函数名 |
| `progress_alignment.py` 直跑无法导入 `experiment` | 1 | 与现有 collector 一致，在脚本模式把仓库根加入 `sys.path` |
| collector/协议组合补丁定位错文件而未应用 | 1 | 拆成按文件补丁，将 watchlist 检查应用到 `qemu_occurrence/collect.py` |
| `bind-source` 正确产出 snapped 后仍返回 2 | 1 | CLI 成功状态扩为 exact/snapped，拒绝状态仍返回 2 |
| 在整个 home 树查找 lbm 输入文件 30 秒超时 | 1 | 改为 workload-builder/SPEC 已知根目录的定向 `rg --files` |
| 当前环境无 `ruff` 模块 | 1 | 改用 `py_compile`、`git diff --check` 和显式 unittest |
| `bind-source` 误把 collector 目录当 `events.json` | 1 | 先运行 `collect-events` 生成 dynamic_execution envelope |
| `lbm` B-native bounded restore 未到 terminal marker | 1 | 保留为 bounded restore evidence，完整 terminal/性能结论保持未验证 |
