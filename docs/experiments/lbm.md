# lbm artifacts

The `lbm` directory contains workload-specific inputs and historical evidence
for cross-build checkpoint experiments. New alignment runs use the repository's
dynamic occurrence workflow:

```sh
checkpoint-align \
  {freeze|collect-events|bind-source|align-progress|materialize-target|validate|report} ...
```

The old interval/BBV reproduction scripts were removed. Existing logs and
result JSON files are retained as immutable provenance and are not executable
inputs to the current aligner.
