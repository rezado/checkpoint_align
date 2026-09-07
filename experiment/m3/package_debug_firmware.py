#!/usr/bin/env python3
"""Package a debug-preserving workload ELF into the existing Linux firmware."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--template-package", required=True, type=Path)
    parser.add_argument("--gcpt-bin", required=True, type=Path)
    parser.add_argument("--sbi-build-dir", required=True, type=Path)
    parser.add_argument("--dts-dir", required=True, type=Path)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--firmware-script", required=True, type=Path)
    parser.add_argument("--default-dtb", default="xiangshan-fpga-noAIA-novec")
    parser.add_argument("--workload-arg", action="append", default=[])
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    package = out_dir / "package"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(args.template_package.resolve(), package, symlinks=True)
    workload = package / "spec" / "libquantum"
    shutil.copy2(args.elf.resolve(), workload)
    workload.chmod(0o755)
    if args.workload_arg:
        run_script = package / "spec" / "run.sh"
        command = "./libquantum " + " ".join(shlex.quote(value) for value in args.workload_arg)
        lines = run_script.read_text().splitlines()
        lines = [
            "spec_cmd=" + shlex.quote(command) if line.startswith("spec_cmd=") else line
            for line in lines
        ]
        run_script.write_text("\n".join(lines) + "\n")

    names = ["."]
    names.extend("./" + str(path.relative_to(package)) for path in sorted(package.rglob("*")))
    rootfs = out_dir / "rootfs.cpio"
    with rootfs.open("wb") as stream:
        subprocess.run(
            ["fakeroot", "cpio", "-o", "-H", "newc"],
            cwd=package,
            input=("\n".join(names) + "\n").encode(),
            stdout=stream,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    env = os.environ.copy()
    env["DEFAULT_DTB"] = args.default_dtb
    subprocess.run(
        [
            "bash",
            str(args.firmware_script.resolve()),
            str(args.gcpt_bin.resolve()),
            str(args.sbi_build_dir.resolve()),
            str(args.dts_dir.resolve()),
            str(args.kernel.resolve()),
            str(out_dir),
        ],
        env=env,
        check=True,
    )
    firmware = out_dir / "fw_payload.bin"
    manifest = {
        "schema_version": 1,
        "elf": str(args.elf.resolve()),
        "elf_sha256": sha256(args.elf.resolve()),
        "rootfs": str(rootfs),
        "rootfs_sha256": sha256(rootfs),
        "firmware": str(firmware),
        "firmware_sha256": sha256(firmware),
        "default_dtb": args.default_dtb,
        "workload_args": args.workload_arg or ["1397", "8"],
        "template_package": str(args.template_package.resolve()),
        "gcpt_bin_sha256": sha256(args.gcpt_bin.resolve()),
        "kernel_sha256": sha256(args.kernel.resolve()),
    }
    (out_dir / "firmware-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
