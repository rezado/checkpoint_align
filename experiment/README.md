# Cross-ELF checkpoint experiment

`cross_elf_checkpoint.py` provides a manifest-driven three-stage workflow:

1. `prepare`: copy each workload's ELF, firmware, command, profile, cluster,
   JSON, and provenance logs into a self-contained suite. If `--workloads` is
   omitted, the common workload set is discovered from both profile roots.
2. `align`: search around the total-instruction-ratio position, compare BBV
   dynamic-count multisets, include same-offset neighboring intervals, and
   reject low-score, low-margin, or non-monotonic candidates.
3. `checkpoint`: generate one target-native checkpoint for an accepted
   candidate, bounded-restore both the original source checkpoint and the new target checkpoint,
   profile the restored windows, and write a structured result.

The method never treats BB IDs, PCs, or raw instruction positions as shared
across ELF files. It is still an experimental BBV/DCFG proxy rather than a
source/IR occurrence tracer, so accepted results remain confidence `L` and
`production_eligible: false`.

## Reusable workflow

```sh
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite experiment/multi-workload \
  --source-root /nfs/home/share/checkpoints_profiles/spec06_gcc16_rv64gcb_260724 \
  --target-root /nfs/home/share/checkpoints_profiles/spec06_gcc16_rva23_novec_260726 \
  --nemu /nfs/home/wujiabin/work/260820_NEMU_paper/NEMU/build/riscv64-nemu-interpreter \
  --gcpt /nfs/home/wujiabin/work/NEMU/resource/gcpt_restore/build/gcpt.bin

# Add `--workloads name1 name2` when only a subset is needed.

# Optional: use descriptive labels instead of A/B; the labels are persisted
# in suite-manifest.json and reused by all later commands.
python3 experiment/cross_elf_checkpoint.py prepare \
  --suite /tmp/my-suite --source-root /path/to/build-one \
  --target-root /path/to/build-two --source-side baseline --target-side candidate

python3 experiment/cross_elf_checkpoint.py align \
  --suite experiment/multi-workload

python3 experiment/cross_elf_checkpoint.py checkpoint \
  --suite experiment/multi-workload \
  --workload mcf --source-point 1

python3 experiment/cross_elf_checkpoint.py report \
  --suite experiment/multi-workload
```

The profile roots use the conventional export layout (`elf/`, `bin/`, `json/`,
`cluster/`, `profiling/`, and `logs/`). Workload names only need to be present
with the required ELF, firmware, JSON, SimPoint, and BBV files on both sides;
optional logs are copied when available. Existing manifests without `sides`
remain compatible and default to `A`/`B`.

Useful controls:

```text
align:      --workloads, --points, --radius, --context,
            --min-score, --min-margin
checkpoint: --warmup, --boot-allowance, --generation-tail,
            --restore-instructions, --min-post-restore-overlap, --timeout
```

`checkpoint` refuses rejected candidates by default. `--force` exists only for
explicit diagnostic experiments and does not upgrade the result confidence.
After restore, every compared window must meet the default 0.5 overlap floor;
otherwise the result is `rejected_post_restore_divergence` even when both
checkpoints are structurally valid and executable.

## Acceptance boundary

A successful checkpoint experiment proves:

- a nonempty B-native Zstandard checkpoint was generated;
- both A and B checkpoints restored with zero process exit and NEMU good state;
- restored BBV windows were captured and compared.

It does not prove workload completion, source/IR occurrence identity, or that a
fixed-length ROI remains equivalent after the bounded restore window.

## Current results

The six-workload alignment scan evaluated 209 A SimPoints. Fifty passed the
experimental score/margin/monotonicity gates; 159 were rejected. Acceptance is
deliberately selective because repetitive phases often have several nearly
identical BBV candidates.

| Workload | A regions | Accepted | Executed checkpoint pair |
|---|---:|---:|---|
| `lbm` | 24 | 9 | A20 -> B20 |
| `mcf` | 22 | 1 | A1 -> B1 |
| `astar_biglakes` | 46 | 2 | A5 -> B5 |
| `libquantum` | 52 | 12 | not run; earliest accepted point is 17,927 |
| `bwaves` | 49 | 16 | not run; earliest accepted point is 2,003 |
| `cactusADM` | 16 | 10 | A7 -> B7 |

All four executed pairs generated a B-native checkpoint and bounded-restored
both A and B for two 20M-instruction BBV windows:

| Pair | Alignment score | Margin | Post-restore overlaps |
|---|---:|---:|---:|
| `lbm A20 -> B20` | 0.932 | 0.885 | 0.799, 0.985 |
| `mcf A1 -> B1` | 0.830 | 0.493 | 0.961, 0.927 |
| `astar_biglakes A5 -> B5` | 0.903 | 0.138 | 0.983, 0.946 |
| `cactusADM A7 -> B7` | 0.868 | 0.415 | 1.000, 0.945 |

Machine-readable aggregate results are in
`multi-workload/results/suite-report.json`.

## PositionAligner roadmap artifacts

The separated M1 BBV locator and its current 209-point replay are under
`position_aligner/`.  The replay reports 62 position-level matches and 147
typed `AMBIGUOUS` rejections.  Every BBV result remains confidence `L` and is
not production eligible; the counts are an unlabeled regression baseline, not
accuracy or coverage against semantic truth.

The M2 DWARF/source work is split by evidence level:

- `dwarf-source/catalog-summary.json` binds static catalogs for all 12 A/B ELF
  artifacts to the current suite manifest;
- `m2/results/validation-summary.json` records a short M-level controlled
  native identity/insertion/ambiguity validation;
- `m2/results/libquantum-small/validation-summary.json` records 95 exact
  dynamic occurrences from a real A/B qemu-user run, capped at confidence L
  because the selected workload functions are symbol-only;
- `m2/validation-summary.json` aggregates the completion gate without
  promoting unsupported results.

M2 remains partial and `m3/shifted-candidate-review.json` remains
`candidate_only`: the supplied reference-input profiles contain no independent
runtime occurrence trace with workload icounts, so none of the eight shifted
BBV candidates has semantic runtime validation.
