#!/usr/bin/env python3
"""Build a tiny RISC-V image and validate NEMU boundary probe semantics."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkpoint_align.position_aligner.materialize import run_nemu_targets, write_nemu_icount_probe_config, write_nemu_occurrence_probe_config
from checkpoint_align.position_aligner.source_boundary import DEFAULT_RNG_SEED


EXPECTED_PCS = [0x80000000, 0x80000002, 0x80000004, 0x80000008, 0x8000000C, 0x8000000E, 0x80000014]


def run_nemu(nemu: Path, image: Path, config: Path, max_instructions: int) -> None:
    completed = subprocess.run(
        [str(nemu), str(image), "-b", "-I", str(max_instructions), "--dont-skip-boot", "--rng-seed", DEFAULT_RNG_SEED, "--semantic-position", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"NEMU fixture failed with status {completed.returncode}")


def validate(nemu: Path, compiler: str, objcopy: str) -> dict:
    fixture = Path(__file__).resolve().parents[1] / "tests" / "position_aligner" / "fixtures"
    with tempfile.TemporaryDirectory(prefix="nemu-boundary-") as directory:
        root = Path(directory)
        elf = root / "exact-boundary.elf"
        image = root / "exact-boundary.bin"
        subprocess.run([compiler, "-nostdlib", "-nostartfiles", "-march=rv64gc", "-mabi=lp64", "-Wl,--no-warn-rwx-segments", f"-Wl,-T,{fixture / 'exact_boundary.ld'}", str(fixture / "exact_boundary.S"), "-o", str(elf)], check=True)
        subprocess.run([objcopy, "-O", "binary", str(elf), str(image)], check=True)

        exact_output = root / "exact.json"
        exact_config = write_nemu_icount_probe_config(root / "exact.txt", {}, {index: index for index in range(7)}, output_path=exact_output, interval_instructions=20)
        run_nemu(nemu, image, exact_config, 100)
        first = exact_output.read_bytes()
        run_nemu(nemu, image, exact_config, 100)
        second = exact_output.read_bytes()
        exact = json.loads(second)
        probes = exact["probes"]
        if not exact.get("complete") or [item["pc"] for item in probes] != EXPECTED_PCS:
            raise AssertionError(f"unexpected exact PCs: {probes}")
        if any(item["observed_icount"] != item["requested_icount"] for item in probes):
            raise AssertionError("exact probe snapped an icount")
        if first != second:
            raise AssertionError("repeated exact probe JSON differs")

        occurrence_output = root / "occurrence.json"
        occurrence_config = write_nemu_occurrence_probe_config(root / "occurrence.txt", {0: EXPECTED_PCS[1]}, {6: (6, 0)}, output_path=occurrence_output, interval_instructions=20)
        run_nemu(nemu, image, occurrence_config, 100)
        occurrence = json.loads(occurrence_output.read_text())
        hit = occurrence["probes"][0]
        if not occurrence.get("complete") or hit["boundary_pc"] != EXPECTED_PCS[6] or hit["marker_pc"] != EXPECTED_PCS[1] or hit["marker_hit_icount"] != 1 or hit["marker_delta_instructions"] != -5:
            raise AssertionError(f"unexpected occurrence snapshot: {hit}")

        incomplete_output = root / "incomplete.json"
        incomplete_config = write_nemu_icount_probe_config(root / "incomplete.txt", {}, {100: 100}, output_path=incomplete_output, interval_instructions=20)
        run_nemu(nemu, image, incomplete_config, 20)
        incomplete = json.loads(incomplete_output.read_text())
        if incomplete.get("complete") or incomplete["probes"][0].get("complete"):
            raise AssertionError("unreached boundary was marked complete")

        targets = [
            {"checkpoint_id": 2, "target": {"anchor_id": "late", "pc": EXPECTED_PCS[6], "occurrence": 0}},
            {"checkpoint_id": 1, "target": {"anchor_id": "early", "pc": EXPECTED_PCS[1], "occurrence": 0}},
        ]
        checkpoint_root = root / "checkpoints"
        materialized = run_nemu_targets(
            targets,
            [str(nemu), str(image), "-b", "-I", "100", "--dont-skip-boot", "--rng-seed", DEFAULT_RNG_SEED, "--checkpoint-format", "zstd"],
            run_manifest={"workload_id": "fixture", "interval_instructions": 20},
            output_dir=checkpoint_root,
        )
        by_id = {int(identifier): item["hit"] for identifier, item in materialized["targets"].items()}
        if not materialized.get("complete") or by_id[1]["workload_icount"] != 1 or by_id[2]["workload_icount"] != 6:
            raise AssertionError(f"hit-key materialization failed: {materialized}")
        archives = sorted(checkpoint_root.glob("**/*memory*.zstd"))
        expected_names = {"_1_memory_.zstd", "_6_memory_.zstd"}
        if {item.name for item in archives} != expected_names:
            raise AssertionError(f"checkpoint filenames do not match hit icounts: {archives}")
        subprocess.run(["zstd", "-t", *map(str, archives)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return {
            "status": "ok",
            "exact_probe_count": len(probes),
            "same_bb_probe_count": 4,
            "compressed_instruction_pcs": [EXPECTED_PCS[0], EXPECTED_PCS[1], EXPECTED_PCS[4]],
            "branch_target_pc": EXPECTED_PCS[6],
            "repeat_sha256": hashlib.sha256(second).hexdigest(),
            "incomplete_verified": True,
            "hit_key_out_of_order_verified": True,
            "checkpoint_icounts": [by_id[1]["workload_icount"], by_id[2]["workload_icount"]],
            "zstd_archives_verified": len(archives),
            "checkpoint_hashes_verified": len(materialized["targets"]),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nemu", type=Path, required=True)
    parser.add_argument("--compiler", default="riscv64-linux-gnu-gcc")
    parser.add_argument("--objcopy", default="riscv64-linux-gnu-objcopy")
    args = parser.parse_args()
    print(json.dumps(validate(args.nemu.resolve(), args.compiler, args.objcopy), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
