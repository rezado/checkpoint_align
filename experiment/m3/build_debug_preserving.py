#!/usr/bin/env python3
"""Build a SPEC CPU2006 libquantum ELF with source-preserving debug metadata.

The reference workload configuration is intentionally left untouched.  This
helper derives a temporary runspec config and records exact build inputs and
output hashes. By default it adds ``-g`` and disables LTO; hybrid mode disables
LTO only for ``gates.c`` while retaining it for the other objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_config(
    source: Path,
    destination: Path,
    output_root: Path,
    jobs: int,
    disable_lto: bool,
    compiler_wrapper: Path | None = None,
    march: str | None = None,
) -> None:
    lines = []
    for line in source.read_text().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("output_root") and "=" in line:
            continue
        if stripped.startswith("makeflags") and "=" in line:
            continue
        lines.append(line)
    text = f"output_root = {output_root}\nmakeflags = -j{jobs}\n\n" + "\n".join(lines) + "\n"
    if compiler_wrapper is not None:
        text = text.replace(
            "CC  = $(COMPILER_PATH)/bin/riscv64-unknown-linux-gnu-gcc -std=gnu89",
            f"CC  = {compiler_wrapper} -std=gnu89",
            1,
        )
    if march is not None:
        text = re.sub(r"-march=[^\s]+", f"-march={march}", text)
    marker = "462.libquantum=default=default=default:\nCPORTABILITY   = -DSPEC_CPU_LINUX\n"
    replacement = marker + "\n# Debug-preserving calibration build: keep source coordinates and disable LTO.\n"
    if marker not in text:
        raise RuntimeError("cannot locate the libquantum portability section in config")
    text = text.replace(marker, replacement, 1)
    text = text.replace("-flto", ("-fno-lto " if disable_lto else "-flto ") + "-g")
    destination.write_text(text)


def run(command: list[str], *, cwd: Path, log: Path, env: dict[str, str]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        stream.write("$ " + " ".join(command) + "\n")
        stream.write("# cwd: " + str(cwd) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"runspec failed with status {result.returncode}; see {log}")


def spec_environment(spec: Path) -> dict[str, str]:
    command = f"cd {shlex.quote(str(spec))} && . ./shrc >/dev/null && env -0"
    raw = subprocess.check_output(["bash", "-lc", command])
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if item:
            key, value = item.split(b"=", 1)
            environment[key.decode()] = value.decode()
    return environment


def write_hybrid_compiler_wrapper(path: Path, compiler: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
real_compiler=__REAL_COMPILER__
has_gates_source=false
for arg in "$@"; do
  if [[ "$arg" == "gates.c" ]]; then
    has_gates_source=true
  fi
done
args=()
for arg in "$@"; do
  if $has_gates_source && [[ "$arg" == "-flto" ]]; then
    continue
  fi
  args+=("$arg")
done
if $has_gates_source; then
  args+=("-fno-lto" "-g")
fi
exec "$real_compiler" "${args[@]}"
""".replace("__REAL_COMPILER__", str(compiler)),
        encoding="utf-8",
    )
    path.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--compiler-root", type=Path, required=True)
    parser.add_argument("--jemalloc-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--march", help="replace every -march value in the derived config")
    parser.add_argument(
        "--keep-lto",
        action="store_true",
        help="retain reference -flto and only add -g (useful for measuring DWARF retention)",
    )
    parser.add_argument(
        "--hybrid-gates",
        action="store_true",
        help="compile only gates.c with -g -fno-lto while retaining LTO elsewhere",
    )
    args = parser.parse_args()

    spec = args.spec.resolve()
    source_config = args.config.resolve()
    compiler_root = args.compiler_root.resolve()
    jemalloc_root = args.jemalloc_root.resolve()
    out_dir = args.out_dir.resolve()
    build_root = out_dir / "runspec"
    config = out_dir / "riscv_gcc16_debug_preserving.cfg"
    log = out_dir / "build.log"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.keep_lto and args.hybrid_gates:
        raise SystemExit("--keep-lto and --hybrid-gates are mutually exclusive")
    wrapper = None
    if args.hybrid_gates:
        wrapper = out_dir / "gcc-hybrid-gates.sh"
        write_hybrid_compiler_wrapper(
            wrapper, compiler_root / "bin" / "riscv64-unknown-linux-gnu-gcc"
        )
    derive_config(
        source_config,
        config,
        build_root / "result",
        args.jobs,
        not args.keep_lto and not args.hybrid_gates,
        wrapper,
        args.march,
    )

    env = os.environ.copy()
    env.update(spec_environment(spec))
    env.update(
        {
            "SPEC": str(spec),
            "LLVM_INSTALL_PATH": str(compiler_root),
            "COMPILER_PATH": str(compiler_root),
            "GNU_RISCV64_PATH": str(compiler_root),
            "JEMALLOC_INSTALL_PATH": str(jemalloc_root),
        }
    )
    runspec = spec / "bin" / "runspec"
    command = [
        str(runspec),
        "--action",
        "build",
        "--config",
        str(config),
        "--tune",
        "base",
        "--noreportable",
        "--iterations",
        "1",
        "462.libquantum",
    ]
    run(command, cwd=spec, log=log, env=env)

    candidates = sorted(
        path for path in build_root.glob("**/exe/libquantum*") if path.is_file() and not path.name.endswith(".md5")
    )
    if not candidates:
        raise RuntimeError(f"runspec completed but no libquantum ELF was found below {build_root}")
    elf = candidates[-1]
    exported = out_dir / "libquantum-debug-preserving.elf"
    shutil.copy2(elf, exported)
    gates_source = spec / "benchspec" / "CPU2006" / "462.libquantum" / "src" / "gates.c"
    metadata = {
        "status": "built",
        "benchmark": "462.libquantum",
        "source": str(spec),
        "source_config": str(source_config),
        "derived_config": str(config),
        "compiler_root": str(compiler_root),
        "jemalloc_root": str(jemalloc_root),
        "march_override": args.march,
        "source_files": {"gates.c": {"path": str(gates_source), "sha256": sha256(gates_source)}},
        "flags_policy": {
            "debug": "-g",
            "lto": "hybrid-gates-no-lto"
            if args.hybrid_gates
            else ("-flto" if args.keep_lto else "-fno-lto"),
        },
        "runspec_elf": str(elf),
        "elf": str(exported),
        "elf_sha256": sha256(exported),
    }
    (out_dir / "build-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
