#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
run_id=$(date +%Y%m%d-%H%M%S)
out="$root/rerun-$run_id"
nemu="$root/tools/riscv64-nemu-interpreter"
gcpt="$root/tools/gcpt.bin"

python3 "$root/run_interval_alignment.py" --experiment "$root"
python3 "$root/analyze_bbv_alignment.py" --experiment "$root" --source-point 20

mkdir -p "$out"
timeout 120s "$nemu" "$root/B/bin/lbm.fw_payload.bin" \
  -D "$out" -w lbm -C aligned-point20 -b -I 540000000 \
  -S "$root/B-aligned-cluster" --cpt-interval 20000000 \
  --warmup-interval 20000000 --checkpoint-format zstd \
  >"$out/generate.out.log" 2>"$out/generate.err.log"

checkpoint="$out/aligned-point20/lbm/20/_20_1.000000_memory_.zstd"
test -s "$checkpoint"
timeout 60s "$nemu" -b -I 40000000 --cpt-restorer "$gcpt" "$checkpoint" \
  >"$out/restore.out.log" 2>"$out/restore.err.log"

grep -q 'Checkpoint done!' "$out/generate.out.log"
grep -Eq 'total guest instructions = 40,000,00[0-9]' "$out/restore.out.log"
grep -q 'NEMU exit with good state' "$out/restore.out.log"
printf 'result_dir=%s\ncheckpoint=%s\n' "$out" "$checkpoint"
