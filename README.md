# Checkpoint Align

Manifest-driven alignment of SimPoint intervals and checkpoints across two
builds of the same workload. The reusable entry point is
`experiment/cross_elf_checkpoint.py`; it supports automatic discovery of the
common workload set and configurable source/target labels.

Typical flow:

```sh
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite /path/to/suite \
  --source-root /path/to/source-profile \
  --target-root /path/to/target-profile \
  --nemu /path/to/riscv64-nemu-interpreter \
  --gcpt /path/to/gcpt.bin
python3 experiment/cross_elf_checkpoint.py align --suite /path/to/suite
python3 experiment/cross_elf_checkpoint.py map-checkpoints \
  --suite /path/to/suite --workload lbm
python3 experiment/cross_elf_checkpoint.py checkpoint-all \
  --suite /path/to/suite --workload lbm --plan-only
python3 experiment/cross_elf_checkpoint.py export-slices \
  --suite /path/to/suite --workload lbm --output /path/to/new-slices
python3 experiment/cross_elf_checkpoint.py checkpoint \
  --suite /path/to/suite --workload lbm --source-point 20
python3 experiment/cross_elf_checkpoint.py report --suite /path/to/suite
```

要为 source 的全部实际 checkpoint 生成 target candidate checkpoint，请在审查
`map-checkpoints` 的结果后执行 `checkpoint-all --include-rejected`。被 alignment
门槛拒绝的 candidate 仍会保持低置信度，不会因为生成了 checkpoint 而升级状态。

The profile export layout is documented in
[`experiment/README.md`](experiment/README.md). Large copied artifacts and
generated results are excluded by [`.gitignore`](.gitignore); only the
reproducible implementation, tests, fixtures, and documentation belong in
this repository.

完整的中文代码说明、使用方法和当前结果见
[`checkpoint-alignment-guide-zh.md`](checkpoint-alignment-guide-zh.md)。
