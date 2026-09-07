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
python3 experiment/cross_elf_checkpoint.py checkpoint \
  --suite /path/to/suite --workload lbm --source-point 20
python3 experiment/cross_elf_checkpoint.py report --suite /path/to/suite
```

The profile export layout is documented in
[`experiment/README.md`](experiment/README.md). Large copied artifacts and
generated results are excluded by [`.gitignore`](.gitignore); only the
reproducible implementation, tests, fixtures, and documentation belong in
this repository.

完整的中文代码说明、使用方法和当前结果见
[`checkpoint-alignment-guide-zh.md`](checkpoint-alignment-guide-zh.md)。
