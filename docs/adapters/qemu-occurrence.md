# QEMU-user occurrence collector

The collector instruments only selected static `ET_EXEC` function entry PCs.
The plugin writes each watch ID as one little-endian `uint32`; Python assigns
zero-based occurrences per anchor and emits the existing `OccurrenceTrace`
schema. QEMU event order is never interpreted as a NEMU instruction count.

Build the plugin against the header installed with the qemu-user binary:

```sh
python3 -m checkpoint_align.qemu_occurrence.collect build-plugin \
  --include /nfs/home/wujiabin/software/riscv-gcc16/include \
  --output local-build/qemu/occurrence-plugin.so
```

Collect and repeat a run:

```sh
python3 -m checkpoint_align.qemu_occurrence.collect collect \
  --qemu /nfs/home/wujiabin/software/riscv-gcc16/bin/qemu-riscv64 \
  --plugin local-build/qemu/occurrence-plugin.so \
  --elf ELF --catalog CATALOG --manifest RUN_INPUT_MANIFEST \
  --output-dir RESULTS --repeat 2 \
  --anchor-name main -- ARGV...
```

The command rejects a nonzero guest exit, missing plugin completion status,
multiple vCPUs, a truncated stream, an unknown watch ID, unmatched decoded
events, or unstable repeated output/occurrences.

For large traces, `--max-per-watch 1` retains only the first dynamic hit of
each selected anchor. This is a sparse synchronization mode; it does not
claim that an omitted later occurrence is aligned.
