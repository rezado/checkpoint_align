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

## Errors Encountered
| Error | Attempt | Resolution |
|---|---:|---|
| 当前目录不是 Git 仓库 | 1 | 本任务明确要求建立本地仓库，完成代码梳理后执行 git init |
| `unittest discover -s experiment` 无法导入相对模块 | 1 | 改用 `python -m unittest experiment.position_aligner.test_bbv experiment.test_dwarf_source` |
