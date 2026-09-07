# Findings

- 两套 profile 都有 `bin/cfg/profiling/cluster/checkpoint/json/stamps`，共有 57 个 workload JSON。
- 两边 profiling 日志显示 interval 为 20,000,000；`namd` A 总指令数为 1722583019975，B 为 1725110728365。
- `namd` A 的 `simpoints0` 有 64 个点，B 有 23 个点，点集合和 cluster id 不可直接对应。
- A/B `namd.elf` SHA-256 不同，但均保留 `.debug_*` 和 `.symtab`，可优先使用 DWARF/符号构造语义锚点。
- A 配置包含 `-march=rv64gc_zba_zbb_zbs_zbc`；B 配置使用 RVA23 相关 `MARCH` 且无向量选项，机器代码布局不可假设一致。
- A 的 point 是动态 interval 编号；报告应将语义锚点及动态 occurrence 作为跨 ELF 坐标，指令数比例只作 sanity check。
