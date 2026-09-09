# Progress

## 2026-08-28

- 已读取 planning-with-files 规范。
- 已检查当前目录不是 Git 仓库，确认不需要 worktree。
- 已核对两套 profile 的目录、配置、JSON、SimPoint 和 ELF 元数据。
- 下一步：生成独立实施报告并复核。
- 已生成 `spec06-cross-binary-simpoint-alignment-report-zh.md`。
- 已复核源/目标路径、57 个 workload、`namd` 指令数和 SimPoint 数量。
- 已澄清 interval 级方案只保证起点量化误差小于 20M，不保证固定 ROI 的语义终点精确对齐。
- 报告结构、代码块、路径和示例说明复核通过；任务完成。
- 最终校验确认报告为 10,587 字节、52 条成对代码围栏，引用的原始综述文件存在。

## 2026-09-07

- 将任务切换为通用 workload checkpoint 对齐入口，读取既有脚本、manifest 和 PositionAligner。
- `cross_elf_checkpoint.py` 新增共有 workload 自动发现、可选 workload 列表、source/target 标签和布局 fallback；align/checkpoint/report 按 manifest 标签运行。
- PositionAligner replay 同步读取 manifest 侧标签；新增根 README 和 `.gitignore`，忽略复制 workload、二进制、profile、日志和生成结果。
- 包内 17 个单元测试通过；临时 `demo` workload 验证自动发现和 `baseline/candidate` 标签 prepare 通过。
- 已执行 `git init`，待完成首个源码提交和最终状态检查。
- 已在 `main` 分支完成首个提交 `1676951`；提交后语法检查和 17 个包内单元测试均通过，工作区干净，忽略规则覆盖复制产物和生成结果。
- 重新执行 `mcf` checkpoint：target checkpoint 生成成功，source/target restore 均 NEMU good state，最低 post-restore overlap 为 0.9144，report 汇总 6 个 workload。
- 修复新 manifest 的复制路径为 suite 相对路径，并保留旧绝对路径的存在性/本地回退；临时 suite 移动后 replay 结果保持一致。
- 新增 `checkpoint-alignment-guide-zh.md`，统一说明代码结构、输入格式、四阶段命令、输出状态、六 workload/四组 checkpoint 结果以及 M1-M3 证据边界；根 README 已增加入口。
- 用户澄清目标为 A 的全部 checkpoint 到 B 的对应关系。已确认 mcf A checkpoint 与 SimPoint 均为同一组 22 个点；开始实现批量映射、生成和断点续跑入口。
- 新增 `map-checkpoints` 和 `checkpoint-all`：实际 source checkpoint 全覆盖检查、一次 NEMU 批量生成、逐对验证、`--include-rejected`、`--plan-only`、`--skip-validation` 和 `--resume`。
- mcf 映射验证为 22/22，22 个唯一 B candidate、无碰撞；1 个推荐映射、21 个 ambiguous candidate。临时双点批量 generation 验证通过；总计 19 个测试通过。
- `report` 已加入 checkpoint correspondence 与 batch 状态汇总；中文总文档和 README 已补充 mcf 全量命令、22 条映射及置信度解释。

## 2026-09-09

- 用户要求从生成结果形成新的切片集合。已确认 mcf 22 个 B checkpoint 均已生成且 batch 完成；验证结果为 1 个通过、21 个 post-restore divergence。
- 开始实现 `export-slices`，以 batch JSON 为权威输入并按验证状态筛选，输出 checkpoint/cluster/manifest。
- 已完成 `export-slices`：导出前逐归档核对 batch 中记录的 SHA-256，支持默认相对 symlink 和显式 copy；输出 B point `simpoints0`、`mapping.tsv` 和 `slice-manifest.json`，不生成权重。
- 已实际导出 mcf `slices-validated`（1 个）和 `slices-all-candidates`（22 个）；22 个链接均可解析，全候选状态保持 1 个 `validated_experimental`、21 个 `rejected_post_restore_divergence`。
- 快速验证：Python compile 通过；20 个 unittest 通过；`git diff --check` 通过。
