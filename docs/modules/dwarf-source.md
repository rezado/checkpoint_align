# DWARF/source-anchor MVP

`../dwarf_source.py` is an offline, build-bound source-anchor indexer and
occurrence-sequence alignment primitive for the M2 roadmap milestone.  It does
not launch NEMU/QEMU,
generate checkpoints, consume `weights0`, or change any existing result.

## What it records

For each ELF the catalog records:

- SHA-256 and a Build-ID when the ELF provides one (the supplied profile ELFs
  currently fall back to the SHA-256 identity);
- `readelf`/`llvm-dwarfdump` commands and tool versions;
- DWARF subprogram and inline ranges with canonical source path, line, column,
  and inline parent context;
- symbol-table function ranges when no equivalent DWARF function covers them;
- source line rows and conservative backward-branch loop candidates;
- a suite manifest hash when `--manifest` or the suite manifest is supplied.

Absolute checkout paths are never used as source identity.  Use
`--path-remap OLD=NEW` when a repository-relative path cannot be recovered
from a DWARF producer path.  Unresolved paths are placed in a deterministic
`external/<hash-prefix>/` namespace.

The current six-workload artifacts are mixed capability inputs.  `lbm`, `mcf`,
`libquantum`, and `bwaves` expose many workload functions through `.symtab` but
their DWARF is dominated by support libraries.  `astar_biglakes` and
`cactusADM` contain substantially richer jemalloc DWARF.  A symbol-only anchor
is intentionally `L` confidence until an independent dynamic marker validates
it.  Static backward branches are `loop_candidate` events, not proof that a
loop executed.

## Catalog commands

Build one catalog:

```sh
python3 -m checkpoint_align.dwarf_source catalog \
  --elf data/multi-workload/workloads/lbm/A/elf/lbm.elf \
  --manifest data/multi-workload/suite-manifest.json \
  --output /tmp/lbm-A.catalog.json
```

Build a compact six-workload summary.  Add `--catalog-dir` when full catalogs
are wanted; they can be large for the C++/jemalloc ELF and are not checked into
this experiment by default.

```sh
python3 -m checkpoint_align.dwarf_source catalog-suite \
  --suite data/multi-workload \
  --output data/results/dwarf-source/catalog-summary.json \
  --no-loops
```

The summary is derived evidence.  It does not claim that a static anchor is a
dynamic occurrence trace.

## Dynamic occurrence collector

The collector consumes a JSON list/object (`pcs`, `samples`, or `trace`) or a
whitespace-separated PC list.  `function-entry` is the default and emits an
event only for an exact function/symbol entry PC.  `anchor-hit` is useful for a
sampled PC stream but has weaker semantics.  Each event has a build-local
`anchor_id`, zero-based local occurrence counter, optional PC and workload
icount, and a semantic key. The trace-level `event_phase` is fixed to
`before_instruction`. Unmatched samples are retained in the
trace metadata.

```sh
python3 -m checkpoint_align.dwarf_source collect \
  --catalog /tmp/lbm-A.catalog.json \
  --trace /path/to/pc-trace.json \
  --output /tmp/lbm-A.occurrences.json
```

This is an offline/native collector interface.  A sampled PC trace does not
prove that every intervening function or loop entry executed; the trace's
`reliability` and `unmatched_samples` fields make that limitation explicit.
Simulator marker triggers remain a later capability (M5).

## Global sequence alignment

Align two occurrence traces independently of any SimPoint query set:

```sh
python3 -m checkpoint_align.dwarf_source align \
  --source-trace /tmp/A.occurrences.json \
  --target-trace /tmp/B.occurrences.json \
  --output /tmp/A-B.alignment.json
```

The dynamic program retains the best two complete monotonic paths, applies an
explicit gap penalty, and reports a global top-1/top-2 margin.  It accepts
same-ID, declared correspondence, or exact semantic-key matches; all other
events become explicit source/target gaps.  A cell budget returns typed
`SEARCH_TRUNCATED` instead of silently accepting a partial path.  The result
contains no weight or checkpoint fields.

## Controlled positive fixture

Generate the small insertion fixture used for deterministic smoke checks:

```sh
python3 -m checkpoint_align.dwarf_source fixture \
  --output data/results/dwarf-source/controlled-positive.json
python3 - <<'PY'
import json
from checkpoint_align.dwarf_source import align_occurrence_sequences

fixture = json.load(open("data/results/dwarf-source/controlled-positive.json"))
result = align_occurrence_sequences(
    fixture["source_events"], fixture["target_events"],
    source_build=fixture["source_build"], target_build=fixture["target_build"],
)
assert result.status == "matched"
assert [(m.source_index, m.target_index) for m in result.matches] == [
    (0, 0), (1, 1), (2, 3), (3, 4)
]
assert result.target_gaps == (2,)
assert result.global_path_margin is not None and result.global_path_margin > 0
PY
```

The fixture is a controlled positive only.  It validates insertion handling,
strict order, and deterministic global margin; it is not a workload ground
truth set and does not upgrade the six-workload artifacts to H/M runtime
evidence.

## Limitations

- No DWARF parser can infer dynamic occurrence counts from static ELF data.
  A complete/native PC or marker trace is required for occurrence evidence.
- `DW_TAG_lexical_block` is not treated as a loop.  GCC builds without an
  explicit loop DIE receive only low-confidence static backward-branch
  candidates.
- The parser intentionally supports the stable textual subset emitted by the
  installed `llvm-dwarfdump`; malformed or producer-specific DIE forms should
  be regenerated with a compatible tool before being used for alignment.
- The sequence aligner is a semantic-event primitive, not a calibrated
  PositionAligner policy.  It does not decide H/M acceptance thresholds,
  validate terminal output, or materialize a B-native checkpoint.
