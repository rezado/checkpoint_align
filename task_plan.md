# Task Plan: General workload checkpoint alignment and local Git setup

## Goal
将当前 checkpoint 对齐入口改为 manifest 驱动，支持不同 workload 和构建侧标签，并在当前目录建立本地 Git 仓库管理代码。

## Phases
- [in_progress] 梳理现有入口和数据布局，确定兼容性边界
- [pending] 实现自动发现 workload、可配置 source/target 标签和通用结果路径
- [pending] 更新文档与忽略规则，初始化本地 Git 并提交可管理源码
- [pending] 运行核心 CLI/单元验证并记录结果

## Errors Encountered
| Error | Attempt | Resolution |
|---|---:|---|
| 当前目录不是 Git 仓库 | 1 | 本任务明确要求建立本地仓库，完成代码梳理后执行 git init |
| `unittest discover -s experiment` 无法导入相对模块 | 1 | 改用 `python -m unittest experiment.position_aligner.test_bbv experiment.test_dwarf_source` |
