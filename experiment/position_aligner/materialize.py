"""B-native target materialization adapters.

This module only writes a target contract and invokes a caller-provided runner;
it never copies A state or fabricates a checkpoint from an event-only trace.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_target(path: str | Path, target: Mapping[str, Any], *, run_manifest: Mapping[str, Any]) -> Path:
    """Write the semantic target that NEMU will materialize from B's start."""

    required = {"build_id", "anchor_id", "occurrence", "event_phase", "pc"}
    missing = sorted(required - set(target))
    if missing:
        raise ValueError(f"target position missing fields: {', '.join(missing)}")
    if target["build_id"] != run_manifest.get("build_id"):
        raise ValueError("target position is not bound to the B run manifest")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "target": dict(target), "run_manifest_sha256": run_manifest.get("manifest_sha256"), "materialization_status": "pending"}
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def write_nemu_config(path: str | Path, target: Mapping[str, Any], *, hit_path: str | Path, interval_instructions: int, context_size: int = 32) -> Path:
    config = Path(path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(("version 1", "mode checkpoint-on-occurrence", f"output {Path(hit_path).resolve()}", "icount-origin 0", f"interval-size {interval_instructions}", f"context-size {context_size}", f"watch 0 {target['pc']}", f"target 0 {target['occurrence']}")) + "\n",
        encoding="utf-8",
    )
    return config


def run_nemu_target(target_path: str | Path, command: list[str], *, run_manifest: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Run NEMU from the B workload start and attach a checkpoint sidecar."""

    target_path = Path(target_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    target_document = json.loads(target_path.read_text(encoding="utf-8"))
    target = target_document["target"]
    hit_path = output / "semantic-hit.json"
    config_path = write_nemu_config(output / "semantic-position.txt", target, hit_path=hit_path, interval_instructions=int(run_manifest["interval_instructions"]))
    command = [*command, "--semantic-position", str(config_path)]
    stdout_path, stderr_path = output / "stdout.log", output / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(command, stdout=stdout, stderr=stderr, check=False)
    result = {
        "schema_version": 1,
        "target": target_document,
        "command": command,
        "exit_status": completed.returncode,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "checkpoint_sidecar": None,
    }
    checkpoints = sorted(output.glob("**/*memory*"))
    hit = json.loads(hit_path.read_text(encoding="utf-8")) if hit_path.is_file() else None
    if completed.returncode == 0 and checkpoints and hit and hit.get("complete"):
        checkpoint = checkpoints[0]
        if hit["event_phase"] != target["event_phase"] or hit["occurrence"] != target["occurrence"] or hit["pc"] != target["pc"]:
            raise RuntimeError("NEMU semantic hit does not match requested target")
        sidecar = output / "checkpoint-sidecar.json"
        sidecar.write_text(json.dumps({"schema_version": 1, "build_id": target["build_id"], "run_id": run_manifest["run_id"], "anchor_id": target["anchor_id"], "occurrence": hit["occurrence"], "event_phase": hit["event_phase"], "pc": hit["pc"], "workload_icount": hit["workload_icount"], "interval": hit["interval"], "offset": hit["offset"], "checkpoint": str(checkpoint), "checkpoint_sha256": _sha256(checkpoint), "marker_consumed": False}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["checkpoint_sidecar"] = str(sidecar)
    (output / "materialization.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
