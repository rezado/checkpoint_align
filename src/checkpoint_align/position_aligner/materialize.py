"""B-native target materialization adapters.

This module only writes a target contract and invokes a caller-provided runner;
it never copies A state or fabricates a checkpoint from an event-only trace.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialization_fingerprint(run_manifest: Mapping[str, Any], command: list[str], target: Mapping[str, Any]) -> str:
    semantic_target = {key: target[key] for key in ("build_id", "anchor_id", "occurrence", "pc")}
    payload = json.dumps({"run_manifest": dict(run_manifest), "command": list(command), "target": semantic_target}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _checkpoint_icount(path: Path) -> int | None:
    match = re.search(r"_(\d+)_memory", path.name)
    return int(match.group(1)) if match else None


def write_target(path: str | Path, target: Mapping[str, Any], *, run_manifest: Mapping[str, Any]) -> Path:
    """Write the semantic target that NEMU will materialize from B's start."""

    if "event_phase" in target:
        raise ValueError("event_phase belongs to the run manifest, not a target position")
    if run_manifest.get("event_phase") != "before_instruction":
        raise ValueError("target run manifest must use before_instruction semantics")
    required = {"build_id", "anchor_id", "occurrence", "pc"}
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


def write_nemu_icount_probe_config(
    path: str | Path,
    watches: Mapping[int | str, int],
    probes: Mapping[int | str, int],
    *,
    output_path: str | Path,
    interval_instructions: int,
    context_size: int = 32,
) -> Path:
    """Write a from-scratch multi-icount semantic probe configuration."""

    config = Path(path)
    config.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "version 2",
        "mode probe-boundary-pcs",
        f"output {Path(output_path).resolve()}",
        "icount-origin 0",
        f"interval-size {interval_instructions}",
        f"context-size {context_size}",
    ]
    rows.extend(f"watch {int(watch_id)} {int(pc)}" for watch_id, pc in sorted(watches.items(), key=lambda item: int(item[0])))
    rows.extend(f"probe {int(probe_id)} {int(icount)}" for probe_id, icount in sorted(probes.items(), key=lambda item: int(item[1])))
    config.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config


def write_nemu_occurrence_probe_config(
    path: str | Path,
    watches: Mapping[int | str, int],
    probes: Mapping[int | str, tuple[int, int]],
    *,
    output_path: str | Path,
    interval_instructions: int,
    context_size: int = 32,
) -> Path:
    """Write the second-pass A marker-occurrence probe contract."""

    config = Path(path)
    config.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "version 2",
        "mode probe-boundary-occurrences",
        f"output {Path(output_path).resolve()}",
        "icount-origin 0",
        f"interval-size {interval_instructions}",
        f"context-size {context_size}",
    ]
    rows.extend(f"watch {int(watch_id)} {int(pc)}" for watch_id, pc in sorted(watches.items(), key=lambda item: int(item[0])))
    rows.extend(f"probe {int(probe_id)} {int(icount)} {int(watch_id)}" for probe_id, (icount, watch_id) in sorted(probes.items(), key=lambda item: int(item[0])))
    config.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config


def write_nemu_batch_config(
    path: str | Path,
    targets: list[Mapping[str, Any]],
    *,
    output_path: str | Path,
    interval_instructions: int,
    context_size: int = 32,
) -> Path:
    """Write an ordered set of semantic occurrence checkpoint targets."""

    config = Path(path)
    config.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "version 1",
        "mode checkpoint-on-occurrences",
        f"output {Path(output_path).resolve()}",
        "icount-origin 0",
        f"interval-size {interval_instructions}",
        f"context-size {context_size}",
    ]
    watch_ids: dict[tuple[str, int], int] = {}
    target_keys: set[tuple[str, int]] = set()
    for item in targets:
        target = item["target"]
        target_key = (str(target["anchor_id"]), int(target["occurrence"]))
        if target_key in target_keys:
            raise ValueError(f"duplicate target position {target_key}")
        target_keys.add(target_key)
        key = (target["anchor_id"], int(target["pc"]))
        if key not in watch_ids:
            watch_ids[key] = len(watch_ids)
            rows.append(f"watch {watch_ids[key]} {int(target['pc'])}")
    for item in targets:
        target = item["target"]
        watch_id = watch_ids[(target["anchor_id"], int(target["pc"]))]
        rows.append(f"checkpoint {int(item['checkpoint_id'])} {watch_id} {int(target['occurrence'])}")
    config.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config


def _checkpoint_command(command: list[str], output: Path, run_manifest: Mapping[str, Any]) -> list[str]:
    return [
        *command,
        "-D",
        str(output.resolve()),
        "-w",
        str(run_manifest["workload_id"]),
        "-C",
        "semantic",
    ]


def run_nemu_target(target_path: str | Path, command: list[str], *, run_manifest: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Run NEMU from the B workload start and attach a checkpoint sidecar."""

    target_path = Path(target_path)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="attempt-", dir=root))
    target_document = json.loads(target_path.read_text(encoding="utf-8"))
    target = target_document["target"]
    if target_document.get("run_manifest_sha256") != run_manifest.get("manifest_sha256"):
        raise ValueError("target contract is not bound to the current run manifest")
    fingerprint = materialization_fingerprint(run_manifest, command, target)
    hit_path = output / "semantic-hit.json"
    config_path = write_nemu_config(output / "semantic-position.txt", target, hit_path=hit_path, interval_instructions=int(run_manifest["interval_instructions"]))
    command = [*_checkpoint_command(command, output, run_manifest), "--semantic-position", str(config_path)]
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
        "run_manifest_sha256": run_manifest.get("manifest_sha256"),
        "run_fingerprint": fingerprint,
    }
    checkpoints = sorted(output.glob("**/*memory*"))
    hit = json.loads(hit_path.read_text(encoding="utf-8")) if hit_path.is_file() else None
    if completed.returncode == 0 and checkpoints and hit and hit.get("complete"):
        checkpoint = checkpoints[0]
        checkpoint_icount = _checkpoint_icount(checkpoint)
        if checkpoint_icount is not None and checkpoint_icount != int(hit["workload_icount"]):
            raise RuntimeError("checkpoint filename icount does not match semantic hit icount")
        if hit["occurrence"] != target["occurrence"] or hit["pc"] != target["pc"]:
            raise RuntimeError("NEMU semantic hit does not match requested target")
        sidecar = output / "checkpoint-sidecar.json"
        sidecar.write_text(json.dumps({"schema_version": 1, "build_id": target["build_id"], "run_id": run_manifest["run_id"], "run_manifest_sha256": run_manifest.get("manifest_sha256"), "run_fingerprint": fingerprint, "anchor_id": target["anchor_id"], "occurrence": hit["occurrence"], "pc": hit["pc"], "workload_icount": hit["workload_icount"], "interval": hit["interval"], "offset": hit["offset"], "checkpoint": str(checkpoint), "checkpoint_sha256": _sha256(checkpoint), "marker_consumed": False}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["checkpoint_sidecar"] = str(sidecar)
    (output / "materialization.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def run_nemu_targets(targets: list[Mapping[str, Any]], command: list[str], *, run_manifest: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Materialize ordered B-native semantic targets in one NEMU execution."""

    if not targets:
        raise ValueError("at least one target is required")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="attempt-", dir=root))
    fingerprints = {str(item["checkpoint_id"]): materialization_fingerprint(run_manifest, command, item["target"]) for item in targets}
    if any(item["target"].get("build_id") != run_manifest.get("build_id") for item in targets):
        raise ValueError("target position is not bound to the current run manifest")
    hit_path = output / "semantic-hits.json"
    config_path = write_nemu_batch_config(
        output / "semantic-position.txt",
        targets,
        output_path=hit_path,
        interval_instructions=int(run_manifest["interval_instructions"]),
    )
    full_command = [*_checkpoint_command(command, output, run_manifest), "--semantic-position", str(config_path)]
    stdout_path, stderr_path = output / "stdout.log", output / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(full_command, stdout=stdout, stderr=stderr, check=False)
    hit_document = json.loads(hit_path.read_text(encoding="utf-8")) if hit_path.is_file() else {}
    hits = {int(hit["id"]): hit for hit in hit_document.get("targets", ()) if hit.get("complete")}
    materialized: dict[str, Any] = {}
    for item in targets:
        checkpoint_id = int(item["checkpoint_id"])
        target = item["target"]
        hit = hits.get(checkpoint_id)
        checkpoint_dir = output / "semantic" / str(run_manifest["workload_id"]) / str(checkpoint_id)
        checkpoints = sorted(checkpoint_dir.glob("**/*memory*")) if checkpoint_dir.is_dir() else []
        if hit and checkpoints:
            if hit["occurrence"] != target["occurrence"] or hit["pc"] != target["pc"]:
                raise RuntimeError(f"NEMU semantic hit {checkpoint_id} does not match requested target")
            checkpoint = checkpoints[0]
            checkpoint_icount = _checkpoint_icount(checkpoint)
            if checkpoint_icount is not None and checkpoint_icount != int(hit["workload_icount"]):
                raise RuntimeError(f"checkpoint {checkpoint_id} filename icount does not match semantic hit icount")
            materialized[str(checkpoint_id)] = {
                "hit": hit,
                "checkpoint": str(checkpoint),
                "checkpoint_sha256": _sha256(checkpoint),
                "run_fingerprint": fingerprints[str(checkpoint_id)],
            }
    result = {
        "schema_version": 1,
        "command": full_command,
        "exit_status": completed.returncode,
        "complete": completed.returncode == 0 and len(materialized) == len(targets) and hit_document.get("complete") is True,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "targets": materialized,
        "run_manifest_sha256": run_manifest.get("manifest_sha256"),
        "attempt_dir": str(output),
    }
    (output / "materialization.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
