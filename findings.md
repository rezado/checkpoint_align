# Findings

- 两套 profile 都有 `bin/cfg/profiling/cluster/checkpoint/json/stamps`，共有 57 个 workload JSON。
- 两边 profiling 日志显示 interval 为 20,000,000；`namd` A 总指令数为 1722583019975，B 为 1725110728365。
- `namd` A 的 `simpoints0` 有 64 个点，B 有 23 个点，点集合和 cluster id 不可直接对应。
- A/B `namd.elf` SHA-256 不同，但均保留 `.debug_*` 和 `.symtab`，可优先使用 DWARF/符号构造语义锚点。
- A 配置包含 `-march=rv64gc_zba_zbb_zbs_zbc`；B 配置使用 RVA23 相关 `MARCH` 且无向量选项，机器代码布局不可假设一致。
- A 的 point 是动态 interval 编号；报告应将语义锚点及动态 occurrence 作为跨 ELF 坐标，指令数比例只作 sanity check。
- 2026-09-07：mcf A 侧有 22 个实际 checkpoint，目录点集合与 A `simpoints0` 的 22 个点完全一致；现有 `align` 已为每点保留 best B candidate，但 `checkpoint` 只能单点执行且默认只允许 accepted candidate。
- 用户要求是覆盖 A 的所有 checkpoint，而非只处理一个 accepted 示例；批处理必须区分“为全部 A 点给出候选关系”和“该关系已通过置信度/restore 验证”。
- 2026-09-09：mcf `checkpoint-all` 已实际完成 22/22 个 B checkpoint（约 5.9 GB），逐对验证为 1 个 `validated_experimental`、21 个 `rejected_post_restore_divergence`；新 slice 导出必须允许按验证状态筛选，不能默认把 21 个失败项包装成可靠切片。
- 新 slice 应使用 B target point 作为 checkpoint 目录/`simpoints0` 位置，沿用 A cluster id 仅作 slice identity；位置对齐不读取或重算权重，导出也不声明 B 代表性权重。
- 2026-09-09：正式切片归档的 JSON schema 由 `scripts/checkpoint/step_metadata.py` 生成，结构为 `{workload: {insts: string, points: {point: weight_string}}}`；归档通常命名 `json/checkpoints_all.json`。用户明确要求 A/B 文件夹内各写 `checkpoints.json`，因此沿用该主体 schema 和指定文件名。
- 成对 A/B 切片中，A 使用原 A point 和 A SimPoint weight；B 使用对齐后的 B point，并继承对应 A cluster 的 weight，以便成对实验采用同一权重。该 weight 不是 B 独立聚类权重，必须在 manifest 中标明；位置匹配算法仍不读取权重。
