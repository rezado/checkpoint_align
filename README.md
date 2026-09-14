# Checkpoint Align

Dynamic, progress-driven alignment of checkpoints across two ELF builds of the
same workload.

## Project layout

```text
src/checkpoint_align/  reusable Python package and CLI
tests/                 unit tests and self-contained fixtures
scripts/               research, build, and validation tools
data/                  workload inputs, protocol fixtures, and run outputs
docs/                  architecture, guides, designs, and reports
```

Install the local package once to expose the `checkpoint-align` command:

```sh
python3 -m pip install -e .
```

The workflow keeps runtime evidence explicit:

```sh
checkpoint-align freeze \
  --elf-a /path/to/a.elf --elf-b /path/to/b.elf \
  --run-manifest-a /path/to/a-run.json \
  --run-manifest-b /path/to/b-run.json \
  --output /tmp/mcf-run.json

checkpoint-align collect-events \
  --run /tmp/mcf-run.json --output /tmp/mcf-events.jsonl
checkpoint-align bind-source \
  --run /tmp/mcf-run.json --events /tmp/mcf-events.jsonl \
  --semantic-position /path/to/source-position.json \
  --output /tmp/mcf-bound.json
checkpoint-align align-progress \
  --source /tmp/mcf-bound.json --target-events /tmp/mcf-target-events.jsonl \
  --output /tmp/mcf-alignment.json
checkpoint-align materialize-target \
  --alignment /tmp/mcf-alignment.json --output /tmp/mcf-target.json
checkpoint-align validate \
  --alignment /tmp/mcf-alignment.json --output /tmp/mcf-validation.json
checkpoint-align report \
  --alignment /tmp/mcf-alignment.json --validation /tmp/mcf-validation.json
```

For the mcf suite, the complete dynamic flow is a single command. It builds
matching semantic watchlists, probes every A checkpoint boundary once, and
materializes all B checkpoints in one ordered NEMU run:

```sh
checkpoint-align checkpoint-suite \
  --suite data/mcf-align \
  --workload-id mcf \
  --source-checkpoints /nfs-nvme/home/share/checkpoints_profiles/spec06_gcc16_rv64gcb_260724/checkpoint/mcf \
  --nemu local-src/NEMU/build/riscv64-nemu-interpreter \
  --output-dir data/results/mcf-semantic-aligned
```

Use `--plan-only` to stop after the A probe and request generation. The
intermediate request document contains the source archive, source
`(anchor, occurrence)`, target semantic position, and dynamic evidence. No
SimPoint number or BBV score is used as an identity.

The lower-level `checkpoint-all` command remains available for an already
constructed request document; it writes one directory per `id`, updates the
batch JSON after every item, and supports `--resume`.

If the request document must be generated separately, use:

```sh
checkpoint-align calibrate-source-checkpoints \
  --source-checkpoints /path/to/A/checkpoint/mcf \
  --source-watchlist-manifest /tmp/mcf-source-watchlist.json \
  --target-watchlist-manifest /tmp/mcf-target-watchlist.json \
  --source-firmware /path/to/A/mcf.fw_payload.bin \
  --nemu local-src/NEMU/build/riscv64-nemu-interpreter \
  --output-dir /tmp/mcf-source-calibration \
  --output /tmp/mcf-checkpoints.json
```

This performs a from-scratch dynamic probe at every checkpoint boundary. The
same semantic occurrence is then used as the B target; B is never inferred
from a raw PC or an interval number.

Positions are identified by `(build_id, anchor_id, occurrence)`. The run manifest
fixes capture semantics to `before_instruction` for the whole trace.
The aligner uses runtime occurrence traces, semantic anchors, monotonicity, and
typed rejection reasons. It never treats PCs, BB IDs, SimPoint interval numbers,
or BBV similarity as cross-ELF identity. A target checkpoint is materialized
only from the target workload's own execution and must carry its checkpoint
sidecar.

The profile and checkpoint artifacts under `data/results/` are ignored
generated data. Older BBV/interval reports remain available as historical
provenance, but their implementation and command-line entry points have been
removed and they are not part of the current alignment contract.

See [`docs/architecture.md`](docs/architecture.md) for artifact schemas and
[`docs/guides/checkpoint-alignment-guide-zh.md`](docs/guides/checkpoint-alignment-guide-zh.md)
for the Chinese workflow guide.
