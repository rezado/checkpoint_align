# PositionAligner

This package exposes the dynamic occurrence alignment interface:

```python
from checkpoint_align.position_aligner import PositionAligner

result = PositionAligner().align(
    {"run": run_a, "events": events_a},
    {"run": run_b, "events": events_b},
    correspondence={},
    source_position=position_a,
)
```

Positions use `(build_id, anchor_id, occurrence)` as their stable identity. The
run manifest fixes capture semantics to `before_instruction`. Alignment combines semantic candidates, explicit gap/occurrence
transforms, a strictly monotonic top-1/top-2 dynamic program, resource budgets,
and typed rejection reasons. The package does not parse BBV files, infer
identity from PCs or interval numbers, or consume `weights0`.

Use `python3 -m checkpoint_align` or
`checkpoint-align` for the dynamic orchestration
commands. Target materialization and validation are deliberately outside the
pure aligner and operate on explicit runtime artifacts.

`calibrate-source-checkpoints` can use a two-pass NEMU probe: first
`probe-boundary-pcs` records exact before-instruction PCs without a large
watchlist, then `probe-boundary-occurrences` watches only selected portable
source markers. The resolver writes `source-resolution.json`; points without a
unique source marker remain typed rejected and are not materialized. Resolved
B positions are candidates until an independently bound dynamic semantic
validation artifact is supplied. Materialization requires that artifact and a
separate `--target-max-instructions` budget for B.

One source position can be emitted at several addresses, so one semantic key
may hold more than one marker row. The boundary PC selects exactly one of them
on the A side; pairing the target row is what becomes undecidable. That case
stays `AMBIGUOUS_SOURCE_MARKER` by default. `--multi-address-policy
identical-elf` instead pairs the target row with the same row-start PC, and is
refused unless both catalogs describe identical ELF content (matching
`artifact_sha256`). Every binding that used it records
`multi_address_disambiguation: identical_elf_address` in its evidence.

Both A passes and B materialization use the same deterministic `--rng-seed`.
The seed is recorded in the resolution/calibration evidence, and cached probe
results are reused only when the NEMU binary, firmware, config, instruction
limit, command, and seed still match. The default displacement limit is one
interval; `0` does not mean unlimited.

The executable NEMU fixture covers sequential, branch, compressed, same-basic-
block multi-probe, incomplete, repeated-run, out-of-order hit-key, checkpoint
filename icount, and zstd integrity behavior:

```sh
NEMU_HOME=$PWD/local-src/NEMU make -C local-src/NEMU -j4
python3 scripts/validate_nemu_boundary_probe.py \
  --nemu local-src/NEMU/build/riscv64-nemu-interpreter
```
