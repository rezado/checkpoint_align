# lbm cross-ELF checkpoint alignment experiment

This directory contains one first-stage checkpoint alignment experiment using
copied SPEC06 `lbm` artifacts from:

- A: `spec06_gcc16_rv64gcb_260724`
- B: `spec06_gcc16_rva23_novec_260726`

Reproduce the mapping, B-native checkpoint generation, and bounded restore with:

```sh
./reproduce.sh
```

`run_interval_alignment.py` first records the total-icount-ratio candidate. That
candidate maps A point 20 to B point 19 only because the A/B total ratio is just
below one; it is rejected rather than used as an alignment result.

`analyze_bbv_alignment.py` compares dynamic count multisets without assuming
that A/B block IDs are shared. For A point 20, B point 20 has 97.30% overlap;
the runner-up B point 19 has 0.72%. The neighboring intervals preserve order.

The original A checkpoint boundary is at `LBM_loadObstacleFile` immediately
after `getc`. The generated B checkpoint stops inside `_IO_getc`, and its saved
return address is the corresponding B `LBM_loadObstacleFile` call site. This
is matching call-context evidence despite the basic-block overshoot difference.

## Observed result

- A/B BBV files: 43,681 rows each.
- A BBV vector sum: 873,620,000,017.
- B BBV vector sum: 873,620,000,000.
- A/B profiling logs both contain `HIT GOOD TRAP`.
- B point 20 checkpoint generated at workload-relative instruction 380,000,002.
- A and B point 20 checkpoints each restored for 40M instructions with zero
  process exit and NEMU good state.
- Post-restore 20M-window overlaps are 79.60% and 1.54%.

The boundary experiment succeeded, but the following fixed-length execution
windows drift. The result therefore remains confidence `L` and is not eligible
as a production-equivalent SimPoint region. It validates the experiment path
and the need for occurrence-level source/IR anchors in the next phase. See
`experiment-result.json` for the exact evidence and limitations.
