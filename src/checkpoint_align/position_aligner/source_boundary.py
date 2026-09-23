"""Resolve checkpoint icounts to portable source occurrences.

The public seam deliberately hides both NEMU passes.  Production injects a
subprocess runner; tests inject an in-memory runner and exercise the same
``resolve`` interface.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Protocol

from checkpoint_align.dwarf_source import Anchor, AnchorCatalog, SourceLocation
from checkpoint_align.position_aligner.materialize import (
    write_nemu_icount_probe_config,
    write_nemu_occurrence_probe_config,
)

DEFAULT_RNG_SEED = hashlib.sha256(b"checkpoint-align").hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CheckpointPoint:
    checkpoint_id: int
    requested_icount: int


@dataclass(frozen=True)
class BoundaryPolicy:
    interval_instructions: int
    max_source_displacement: int | None = None
    multi_address_policy: str = "reject"

    @property
    def displacement_limit(self) -> int:
        return self.interval_instructions if self.max_source_displacement is None else self.max_source_displacement

    @property
    def pair_multi_address_by_pc(self) -> bool:
        return self.multi_address_policy == "identical-elf"


@dataclass(frozen=True)
class OccurrenceProbeRequest:
    checkpoint_id: int
    requested_icount: int
    semantic_key: str
    marker_pc: int


@dataclass(frozen=True)
class ProbeRun:
    document: Mapping[str, Any]
    artifact: str
    sha256: str


class SourceProbeRunner(Protocol):
    def probe_boundaries(self, points: tuple[CheckpointPoint, ...]) -> ProbeRun: ...

    def probe_occurrences(self, points: tuple[OccurrenceProbeRequest, ...]) -> ProbeRun: ...


@dataclass(frozen=True)
class SourceBinding:
    checkpoint_id: int
    status: str
    reason: str | None = None
    semantic_key: str | None = None
    source_marker: dict[str, Any] | None = None
    target_marker: dict[str, Any] | None = None
    requested_icount: int | None = None
    boundary_pc: int | None = None
    marker_hit_icount: int | None = None
    marker_delta_instructions: int | None = None
    occurrence: int | None = None
    boundary_observation_sha256: str | None = None
    occurrence_observation_sha256: str | None = None
    multi_address_disambiguation: str | None = None


@dataclass(frozen=True)
class SourceBindingBatch:
    items: tuple[SourceBinding, ...]
    boundary_artifact: str
    occurrence_artifact: str | None

    @property
    def resolved(self) -> tuple[SourceBinding, ...]:
        return tuple(item for item in self.items if item.status == "resolved")


class NemuSourceProbeRunner:
    """Subprocess adapter for the two from-scratch NEMU probe passes."""

    def __init__(
        self,
        *,
        nemu: str | Path,
        firmware: str | Path,
        output_dir: str | Path,
        interval_instructions: int,
        max_instructions: int,
        context_size: int = 32,
        timeout: int = 86_400,
        force: bool = False,
        rng_seed: str = DEFAULT_RNG_SEED,
    ) -> None:
        self.nemu = Path(nemu)
        self.firmware = Path(firmware)
        self.output_dir = Path(output_dir)
        self.interval_instructions = interval_instructions
        self.max_instructions = max_instructions
        self.context_size = context_size
        self.timeout = timeout
        self.force = force
        self.rng_seed = rng_seed

    @staticmethod
    def _probe_ids(document: Mapping[str, Any]) -> set[int]:
        return {int(item["id"]) for item in document.get("probes", ())}

    def _run(self, directory: Path, config: Path, output: Path, expected_ids: set[int]) -> ProbeRun:
        command = [str(self.nemu), str(self.firmware), "-b", "-I", str(self.max_instructions), "--rng-seed", self.rng_seed, "--semantic-position", str(config)]
        manifest_path = directory / "run-manifest.json"
        manifest = {
            "schema_version": 1,
            "command": command,
            "nemu_sha256": _file_sha256(self.nemu),
            "firmware_sha256": _file_sha256(self.firmware),
            "config_sha256": _file_sha256(config),
            "rng_seed": self.rng_seed,
        }
        existing_manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
        existing = json.loads(output.read_text()) if output.is_file() else None
        document = existing if existing_manifest == manifest and existing and self._probe_ids(existing) == expected_ids and not self.force else None
        if document is None:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                completed = subprocess.run(command, stdout=stdout, stderr=stderr, check=False, timeout=self.timeout)
            if completed.returncode != 0 or not output.is_file():
                raise RuntimeError(f"NEMU source probe failed with status {completed.returncode}")
            document = json.loads(output.read_text())
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return ProbeRun(document, str(output), hashlib.sha256(output.read_bytes()).hexdigest())

    def probe_boundaries(self, points: tuple[CheckpointPoint, ...]) -> ProbeRun:
        directory = self.output_dir / "boundary-probe"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "exact-boundaries.json"
        config = write_nemu_icount_probe_config(
            directory / "semantic-position.txt",
            {},
            {point.checkpoint_id: point.requested_icount for point in points},
            output_path=output,
            interval_instructions=self.interval_instructions,
            context_size=self.context_size,
        )
        return self._run(directory, config, output, {point.checkpoint_id for point in points})

    def probe_occurrences(self, points: tuple[OccurrenceProbeRequest, ...]) -> ProbeRun:
        directory = self.output_dir / "occurrence-probe"
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "source-occurrences.json"
        watch_ids: dict[tuple[str, int], int] = {}
        for point in points:
            watch_ids.setdefault((point.semantic_key, point.marker_pc), len(watch_ids))
        watches = {watch_id: marker_pc for (_, marker_pc), watch_id in watch_ids.items()}
        probes = {
            point.checkpoint_id: (point.requested_icount, watch_ids[(point.semantic_key, point.marker_pc)])
            for point in points
        }
        config = write_nemu_occurrence_probe_config(
            directory / "semantic-position.txt",
            watches,
            probes,
            output_path=output,
            interval_instructions=self.interval_instructions,
            context_size=self.context_size,
        )
        return self._run(directory, config, output, {point.checkpoint_id for point in points})


class _MarkerIndex:
    def __init__(self, catalog: AnchorCatalog) -> None:
        self.catalog = catalog
        self.catalog_sha256 = _json_sha256(catalog.to_dict())
        self.rows = tuple(sorted(catalog.line_entries, key=lambda row: int(row.get("pc", 0))))
        self.starts = tuple(int(row.get("pc", 0)) for row in self.rows)
        self.functions = sorted(
            [
                (address_range.start, address_range.end, anchor)
                for anchor in catalog.anchors
                if anchor.kind in {"function", "symbol", "inline"}
                for address_range in anchor.ranges
            ],
            key=lambda item: (item[0], item[1], item[2].anchor_id),
        )

    def _line_at(self, pc: int) -> tuple[dict[str, Any], int, int] | None:
        index = bisect.bisect_right(self.starts, pc) - 1
        if index < 0:
            return None
        row = self.rows[index]
        start = int(row.get("pc", 0))
        end = int(self.rows[index + 1].get("pc", start + 1)) if index + 1 < len(self.rows) else start + 1
        return (row, start, max(end, start + 1)) if start <= pc < max(end, start + 1) else None

    def _function_context(self, pc: int) -> tuple[Anchor | None, str | None]:
        candidates = [item for item in self.functions if item[0] <= pc < item[1]]
        if not candidates:
            return None, None
        # Inline context is the most specific source identity; a DWARF function
        # wins over its symtab alias, which is a low-confidence fallback.
        priority = {"inline": 0, "function": 1}
        candidates.sort(key=lambda item: (item[1] - item[0], priority.get(item[2].kind, 2), item[2].anchor_id))
        best = candidates[0][2]
        equally_specific = [item for item in candidates if item[1] - item[0] == candidates[0][1] - candidates[0][0]]
        # Two anchors over the identical address range describe one code region:
        # a DWARF function against its symtab alias, or a self-recursive inline
        # instance against the function it expands.  Candidates are already
        # ordered inline > function > symbol, so collapse each range to its
        # highest-priority entry and only treat distinct ranges as ambiguity.
        by_range: dict[tuple[int, int], Anchor] = {}
        for start, end, anchor in equally_specific:
            by_range.setdefault((start, end), anchor)
        contexts = {(item.name or item.linkage_name or "", item.inline_chain) for item in by_range.values()}
        if len(contexts) != 1:
            return None, "UNSUPPORTED_INLINE_CONTEXT"
        return best, None

    def marker(self, pc: int) -> tuple[dict[str, Any] | None, str | None, str | None]:
        line = self._line_at(pc)
        if line is None:
            return None, None, "NO_SOURCE_MAPPING"
        row, line_start, line_end = line
        source = SourceLocation.from_dict(row.get("source"))
        if source is None or source.path is None or source.line is None:
            return None, None, "NO_SOURCE_MAPPING"
        anchor, reason = self._function_context(pc)
        if reason:
            return None, None, reason
        if anchor is None:
            return None, None, "NO_SOURCE_MAPPING"
        function = anchor.name or anchor.linkage_name or ""
        key = "|".join((source.path, str(source.line), str(source.column or 0), str(source.discriminator or 0), function, *anchor.inline_chain))
        anchor_start = max(item.start for item in anchor.ranges if item.start <= pc < item.end)
        anchor_end = min(item.end for item in anchor.ranges if item.start <= pc < item.end)
        if not anchor_start <= line_start < anchor_end:
            return None, None, "NO_SOURCE_MAPPING"
        marker_id = f"{self.catalog.build_identity}:source:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{line_start:x}"
        marker = {
            "anchor_id": marker_id,
            "pc": line_start,
            "range": {"start": max(line_start, anchor_start), "end": min(line_end, anchor_end)},
            "source": source.to_dict(),
            "function": function,
            "inline_chain": list(anchor.inline_chain),
            "semantic_key": key,
            "catalog_sha256": self.catalog_sha256,
        }
        if not marker["range"]["start"] <= pc < marker["range"]["end"]:
            return None, None, "NO_SOURCE_MAPPING"
        return marker, key, None


class SourceBoundaryResolver:
    """Deep module for exact source binding across both dynamic probe passes."""

    def __init__(self, runner: SourceProbeRunner) -> None:
        self.runner = runner

    @staticmethod
    def _points(values: Iterable[CheckpointPoint | Mapping[str, Any]]) -> tuple[CheckpointPoint, ...]:
        points = tuple(
            item if isinstance(item, CheckpointPoint) else CheckpointPoint(int(item["checkpoint_id"] if "checkpoint_id" in item else item["id"]), int(item["requested_icount"]))
            for item in values
        )
        if not points or len({item.checkpoint_id for item in points}) != len(points):
            raise ValueError("checkpoint ids must be non-empty and unique")
        return tuple(sorted(points, key=lambda item: (item.requested_icount, item.checkpoint_id)))

    @staticmethod
    def _probes(run: ProbeRun, mode: str) -> dict[int, Mapping[str, Any]]:
        document = run.document
        if document.get("schema_version") != 2 or document.get("mode") != mode:
            raise ValueError(f"unexpected {mode} probe schema")
        if document.get("event_phase") != "before_instruction":
            raise ValueError("source probe must use before_instruction semantics")
        probes = tuple(document.get("probes", ()))
        by_id = {int(item["id"]): item for item in probes}
        if len(by_id) != len(probes):
            raise ValueError(f"duplicate probe id in {mode} artifact")
        return by_id

    def resolve(
        self,
        checkpoint_points: Iterable[CheckpointPoint | Mapping[str, Any]],
        source_run: Mapping[str, Any],
        source_catalog: AnchorCatalog,
        target_catalog: AnchorCatalog,
        policy: BoundaryPolicy,
    ) -> SourceBindingBatch:
        if source_run.get("event_phase") != "before_instruction":
            raise ValueError("source run must use before_instruction semantics")
        if int(source_run.get("interval_instructions", policy.interval_instructions)) != policy.interval_instructions:
            raise ValueError("source run interval does not match boundary policy")
        if policy.pair_multi_address_by_pc and source_catalog.artifact_sha256 != target_catalog.artifact_sha256:
            raise ValueError("multi-address policy identical-elf requires identical source and target ELF content")
        points = self._points(checkpoint_points)
        boundary_run = self.runner.probe_boundaries(points)
        boundary_probes = self._probes(boundary_run, "probe-boundary-pcs")
        source_index = _MarkerIndex(source_catalog)
        target_index = _MarkerIndex(target_catalog)

        source_by_key: dict[str, list[dict[str, Any]]] = {}
        for row in source_index.rows:
            marker, key, _ = source_index.marker(int(row.get("pc", 0)))
            if marker is not None and key is not None:
                source_by_key.setdefault(key, []).append(marker)
        source_by_key = {key: list({(item["anchor_id"], item["pc"]): item for item in values}.values()) for key, values in source_by_key.items()}
        target_by_key: dict[str, list[dict[str, Any]]] = {}
        for row in target_index.rows:
            marker, key, _ = target_index.marker(int(row.get("pc", 0)))
            if marker is not None and key is not None:
                target_by_key.setdefault(key, []).append(marker)
        target_by_key = {key: list({(item["anchor_id"], item["pc"]): item for item in values}.values()) for key, values in target_by_key.items()}

        items: list[SourceBinding] = []
        occurrence_requests: list[OccurrenceProbeRequest] = []
        pair_by_address = policy.pair_multi_address_by_pc
        for point in points:
            probe = boundary_probes.get(point.checkpoint_id)
            base = SourceBinding(point.checkpoint_id, "rejected", requested_icount=point.requested_icount, boundary_observation_sha256=boundary_run.sha256)
            if probe is None or not probe.get("complete"):
                items.append(replace(base, reason="BOUNDARY_NOT_REACHED"))
                continue
            observed = int(probe.get("observed_icount", probe.get("workload_icount", -1)))
            boundary_pc = int(probe["pc"])
            base = replace(base, boundary_pc=boundary_pc)
            if observed != point.requested_icount:
                items.append(replace(base, reason="INEXACT_BOUNDARY_OBSERVATION"))
                continue
            source_marker, key, reason = source_index.marker(boundary_pc)
            if reason or source_marker is None or key is None:
                items.append(replace(base, reason=reason or "NO_PORTABLE_MARKER"))
                continue
            # A single source line can be emitted at several addresses, so one
            # semantic key may hold several marker rows.  The boundary PC still
            # selects exactly one of them; the key only becomes undecidable when
            # the two builds must be paired without shared code layout.
            disambiguation: str | None = None
            if len(source_by_key.get(key, [])) != 1:
                if not pair_by_address:
                    items.append(replace(base, reason="AMBIGUOUS_SOURCE_MARKER", semantic_key=key, source_marker=source_marker))
                    continue
                disambiguation = "identical_elf_address"
            targets = target_by_key.get(key, [])
            if not targets:
                items.append(replace(base, reason="NO_PORTABLE_MARKER", semantic_key=key, source_marker=source_marker))
                continue
            if len(targets) == 1:
                target_marker = targets[0]
            elif pair_by_address:
                matched = [item for item in targets if int(item["pc"]) == int(source_marker["pc"])]
                if len(matched) != 1:
                    items.append(replace(base, reason="AMBIGUOUS_TARGET_MARKER", semantic_key=key, source_marker=source_marker))
                    continue
                target_marker = matched[0]
                disambiguation = "identical_elf_address"
            else:
                items.append(replace(base, reason="AMBIGUOUS_TARGET_MARKER", semantic_key=key, source_marker=source_marker))
                continue
            pending = replace(base, status="pending_occurrence", reason=None, semantic_key=key, source_marker=source_marker, target_marker=target_marker, multi_address_disambiguation=disambiguation)
            items.append(pending)
            occurrence_requests.append(OccurrenceProbeRequest(point.checkpoint_id, point.requested_icount, key, int(source_marker["pc"])))

        if not occurrence_requests:
            return SourceBindingBatch(tuple(items), boundary_run.artifact, None)

        occurrence_run = self.runner.probe_occurrences(tuple(occurrence_requests))
        occurrence_probes = self._probes(occurrence_run, "probe-boundary-occurrences")
        resolved: list[SourceBinding] = []
        for item in items:
            if item.status != "pending_occurrence":
                resolved.append(item)
                continue
            probe = occurrence_probes.get(item.checkpoint_id)
            base = replace(item, status="rejected", occurrence_observation_sha256=occurrence_run.sha256)
            if probe is None or not probe.get("complete"):
                resolved.append(replace(base, reason="MARKER_NOT_ENTERED"))
                continue
            if int(probe.get("boundary_pc", -1)) != item.boundary_pc:
                resolved.append(replace(base, reason="NONDETERMINISTIC_SOURCE_RUN"))
                continue
            marker_pc = int(probe.get("marker_pc", probe.get("pc", -1)))
            marker_hit_icount = int(probe.get("marker_hit_icount", probe.get("workload_icount", -1)))
            delta = marker_hit_icount - int(item.requested_icount)
            if marker_pc != int(item.source_marker["pc"]):
                resolved.append(replace(base, reason="MARKER_PC_MISMATCH"))
                continue
            marker_range = item.source_marker["range"]
            if not int(marker_range["start"]) <= int(item.boundary_pc) < int(marker_range["end"]):
                resolved.append(replace(base, reason="BOUNDARY_OUTSIDE_MARKER"))
                continue
            if abs(delta) > policy.displacement_limit:
                resolved.append(replace(base, reason="SOURCE_DISPLACEMENT_EXCEEDED", marker_hit_icount=marker_hit_icount, marker_delta_instructions=delta))
                continue
            resolved.append(replace(base, status="resolved", reason=None, marker_hit_icount=marker_hit_icount, marker_delta_instructions=delta, occurrence=int(probe["occurrence"])))

        collisions: dict[tuple[str, int], list[int]] = {}
        for index, item in enumerate(resolved):
            if item.status == "resolved":
                collisions.setdefault((str(item.target_marker["anchor_id"]), int(item.occurrence)), []).append(index)
        for indexes in collisions.values():
            if len(indexes) > 1:
                for index in indexes:
                    resolved[index] = replace(resolved[index], status="rejected", reason="TARGET_POSITION_COLLISION")
        return SourceBindingBatch(tuple(resolved), boundary_run.artifact, occurrence_run.artifact)
