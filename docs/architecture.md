# Dynamic cross-ELF checkpoint alignment

`checkpoint-align` is the sole orchestration entry point. It separates
run freezing, sparse event collection, source binding, dynamic alignment,
target materialization, and validation into explicit artifacts:

```text
freeze -> collect-events -> bind-source -> align-progress
       -> materialize-target -> validate -> report
```

The protocol is implemented in `src/checkpoint_align/position_aligner/protocol.py`. A `Position`
contains the build, semantic anchor, dynamic occurrence, and event phase. A
`Correspondence` records the target position, confidence, score, margin, and
typed status. `PositionAligner` requires compatible run manifests and returns
an `AlignmentResult` without consuming SimPoint weights.

`src/checkpoint_align/qemu_occurrence/collect.py` is the sparse runtime adapter. It records marker
events with sequence numbers and workload-relative instruction counts when the
runner provides them. `src/checkpoint_align/dwarf_source.py` supplies static anchor catalogs and
source-side occurrence binding; static metadata alone is not accepted as
dynamic identity.

`materialize-target` starts the target workload from its own run configuration.
It does not copy source registers, memory, PCs, or checkpoint bytes. Successful
materialization requires a target checkpoint sidecar; `validate` keeps restore,
cross-build, and coverage outcomes separate and never upgrades an ambiguous
mapping implicitly.

`checkpoint-all` wraps the same dynamic path for multiple A positions. It
consumes a `{"checkpoints": [{"id": ..., "request": {...}}]}` document,
persists a result after every item, and supports `--plan-only` and `--resume`.
Requests must contain dynamic occurrence evidence; old SimPoint/interval
numbers are not converted implicitly.

`calibrate-source-checkpoints` probes all A checkpoint boundaries from one
from-scratch dynamic execution and emits a request for each resolved semantic
occurrence. `checkpoint-suite` then feeds those requests to one ordered B
execution, which writes every B-native checkpoint. This avoids both static
interval inference and repeated per-checkpoint re-execution.

Generated results under `data/results/` are ignored by Git. Historical BBV/interval
reports may remain for provenance, but the old implementation and its CLI are
not supported or importable.
