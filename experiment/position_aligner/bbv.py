"""BBV-based position alignment primitives.

This module is deliberately independent from ``cross_elf_checkpoint.py``.  It
implements the M1 experimental locator only: profile rows are indexed once,
candidate rows are scored, and a sparse monotonic dynamic program chooses a
global path.  It does not read SimPoint weights or create checkpoints.

The public helpers are :func:`build_index` and :func:`align`.  Inputs are kept
plain (paths, dictionaries, and small dataclasses are accepted) so the module
can be used by the protocol and DWARF layers without coupling them to a
runner.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
POLICY_VERSION = "bbv-baseline-v1"


# Position-level reasons are intentionally strings.  They remain readable in
# JSON reports and can be extended without breaking callers that do not import
# an Enum.
NO_ANCHOR = "NO_ANCHOR"
NO_CANDIDATE = "NO_CANDIDATE"
SEARCH_TRUNCATED = "SEARCH_TRUNCATED"
AMBIGUOUS = "AMBIGUOUS"
CROSSING = "CROSSING"
MARKER_GAP_TOO_LARGE = "MARKER_GAP_TOO_LARGE"
LOW_FIDELITY = "LOW_FIDELITY"
OUT_OF_TRACE = "OUT_OF_TRACE"
POST_ALIGN_DIVERGENCE = "POST_ALIGN_DIVERGENCE"
INVALID_SOURCE_ORDER = "INVALID_SOURCE_ORDER"

# Batch-level reasons.
INCOMPATIBLE_RUN = "INCOMPATIBLE_RUN"
NONDETERMINISTIC_TRACE = "NONDETERMINISTIC_TRACE"
ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
EVIDENCE_COLLECTION_FAILED = "EVIDENCE_COLLECTION_FAILED"


@dataclass(frozen=True)
class AlignmentPolicy:
    """Versioned knobs for the M1 BBV locator.

    The defaults are deliberately experimental.  In particular, a BBV-only
    match is reported at confidence ``L`` and never promoted to H/M.  Threshold
    values are policy data and are emitted in every result for reproducibility.
    """

    version: str = POLICY_VERSION
    initial_window: int = 8
    max_window: int = 128
    expansion_factor: float = 2.0
    context_radius: int = 2
    candidate_top_k: int = 8
    max_candidates_per_position: int = 64
    min_score: float = 0.55
    min_global_margin: float = 0.10
    # Gap costs are expressed in normalized score units, not instructions.
    source_gap_penalty: float = 0.70
    target_gap_penalty: float = 0.002
    warp_penalty: float = 0.05
    # A BBV point is an experimental snap, never an H/M exact anchor.
    max_snap_distance: int = 0
    tie_epsilon: float = 1e-12
    tie_rule: str = "score_desc_target_lexicographic"
    max_events: int | None = 1_000_000
    max_trace_bytes: int | None = 512 * 1024 * 1024
    max_memory_bytes: int | None = 512 * 1024 * 1024
    max_seconds: float | None = 30.0
    # The feature weights are normalized over available components.
    feature_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "anchor_identity": 0.20,
            "sequence_context": 0.25,
            "local_bbv": 0.45,
            "progress_consistency": 0.10,
            "call_loop_context": 0.0,
        }
    )

    def __post_init__(self) -> None:
        if self.initial_window < 0 or self.max_window < self.initial_window:
            raise ValueError("window bounds must be non-negative and ordered")
        if self.expansion_factor <= 1.0 and self.initial_window != self.max_window:
            raise ValueError("expansion_factor must exceed one when expansion is enabled")
        if self.context_radius < 0 or self.candidate_top_k < 1:
            raise ValueError("context_radius and candidate_top_k must be non-negative/positive")
        if self.max_candidates_per_position < 1:
            raise ValueError("max_candidates_per_position must be positive")
        if self.min_global_margin < 0 or self.min_score < 0:
            raise ValueError("score thresholds must be non-negative")
        if self.max_snap_distance < 0:
            raise ValueError("max_snap_distance must be non-negative")
        if self.max_memory_bytes is not None and self.max_memory_bytes < 1:
            raise ValueError("max_memory_bytes must be positive or None")
        if not self.tie_rule:
            raise ValueError("tie_rule must not be empty")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["feature_weights"] = dict(self.feature_weights)
        return value


@dataclass(frozen=True)
class BbvEvent:
    """One profile interval represented by its dynamic count vector."""

    index: int
    workload_icount: int
    counts: tuple[int, ...]


@dataclass(frozen=True)
class RunSpec:
    """Normalized build/run identity and BBV evidence location."""

    build_id: str
    bbv_path: Path
    manifest: Mapping[str, Any]
    manifest_hash: str
    interval_instructions: int
    run_id: str | None = None
    icount_domain: str = "workload_relative_instructions"
    interval_index_base: int = 0
    input_fingerprint: str | None = None
    workload_id: str | None = None
    total_instructions: int | None = None
    trace_bytes: int = 0
    inline_events: tuple[tuple[int, ...], ...] | None = None
    # Artifact identity is separate from the human-friendly build label.  A
    # caller may provide an ELF SHA-256 or a tool-generated Build-ID; keeping
    # it on the normalized run makes provenance auditable without changing the
    # public ``build_id`` selector used by ``align``.
    artifact_sha256: str | None = None
    build_identity: str | None = None


@dataclass
class PositionIndex:
    """Reusable per-build BBV index returned by :func:`build_index`."""

    runs: dict[str, RunSpec]
    events: dict[str, tuple[BbvEvent, ...]]
    policy: AlignmentPolicy
    workload_id: str | None
    workload_manifest_hash: str | None
    budget: dict[str, Any]
    schema_version: int = SCHEMA_VERSION

    def __getitem__(self, key: str) -> Any:
        # A small mapping facade makes the object convenient in notebooks and
        # keeps compatibility with callers that expect ``index["runs"]``.
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workload_id": self.workload_id,
            "workload_manifest_hash": self.workload_manifest_hash,
            "policy": self.policy.as_dict(),
            "runs": {
                build_id: {
                    "build_id": run.build_id,
                    "run_id": run.run_id,
                    "bbv_path": str(run.bbv_path),
                    "manifest_hash": run.manifest_hash,
                    "interval_instructions": run.interval_instructions,
                    "icount_domain": run.icount_domain,
                    "interval_index_base": run.interval_index_base,
                    "total_instructions": run.total_instructions,
                    "trace_bytes": run.trace_bytes,
                    "event_count": len(self.events.get(build_id, ())),
                    "artifact_sha256": run.artifact_sha256,
                    "build_identity": run.build_identity,
                }
                for build_id, run in self.runs.items()
            },
            "budget": self.budget,
        }


@dataclass(frozen=True)
class _PathState:
    score: float
    # Each tuple is (source sequence index, target interval index or None).
    mappings: tuple[tuple[int, int | None], ...]
    last_target: int
    last_source: int | None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8"))


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _workload_id(workload: Any) -> str | None:
    if workload is None:
        return None
    if isinstance(workload, str):
        candidate = Path(workload)
        if candidate.is_file():
            try:
                return _workload_id(_read_json(candidate))
            except (OSError, ValueError, TypeError):
                pass
        return workload
    if isinstance(workload, Path):
        try:
            data = _read_json(workload)
        except (OSError, ValueError, TypeError):
            return workload.stem
        return _workload_id(data)
    if isinstance(workload, Mapping):
        value = _first(workload, "workload_id", "workload", "id", "name")
        return str(value) if value is not None else None
    return str(workload)


def _manifest_hash(manifest: Mapping[str, Any], path: Path | None = None) -> str:
    # Hash the manifest bytes when a path is supplied so provenance is exact;
    # dictionaries use canonical JSON to avoid key-order noise.
    if path is not None and path.is_file():
        return _sha256_file(path)
    return _hash_json(manifest)


def _normalise_policy(policy: AlignmentPolicy | Mapping[str, Any] | None,
                      policy_version: str | None) -> AlignmentPolicy:
    if policy is None:
        # A caller may label an otherwise default policy with a local version
        # while calibration is still in progress.  The version is provenance,
        # not a registry lookup, so unknown labels remain explicit and usable.
        return AlignmentPolicy(version=policy_version or POLICY_VERSION)
    if isinstance(policy, AlignmentPolicy):
        if policy_version and policy.version != policy_version:
            raise ValueError("policy_version does not match policy.version")
        return policy
    fields = dict(policy)
    if policy_version is not None:
        fields.setdefault("version", policy_version)
    return AlignmentPolicy(**fields)


def _normalise_run(spec: Any, default_interval: int | None = None) -> RunSpec:
    """Convert a path/dataclass/dict into a :class:`RunSpec`."""

    if isinstance(spec, RunSpec):
        return spec
    if is_dataclass(spec):
        spec = asdict(spec)
    elif not isinstance(spec, (str, Path, Mapping)) and hasattr(spec, "__dict__"):
        # M0 protocol objects are intentionally accepted without importing
        # their module.  This keeps the adapter plain and avoids a dependency
        # cycle between protocol and evidence layers.
        spec = vars(spec)
    if isinstance(spec, (str, Path)):
        path = Path(spec)
        manifest = _read_json(path) if path.suffix.lower() == ".json" else {}
        if path.suffix.lower() == ".json":
            bbv = _first(manifest, "bbv_path", "bbv", "simpoint_bbv")
            if bbv is None:
                raise ValueError(f"run manifest has no BBV path: {path}")
            bbv_path = Path(bbv)
            if not bbv_path.is_absolute():
                bbv_path = path.parent / bbv_path
        else:
            bbv_path = path
            manifest = {}
        return _normalise_run({"bbv_path": bbv_path, "manifest": manifest}, default_interval)

    if not isinstance(spec, Mapping):
        raise TypeError(f"unsupported run specification: {type(spec)!r}")
    nested_manifest = spec.get("manifest")
    manifest_path: Path | None = None
    if nested_manifest is None:
        manifest_value = _first(spec, "manifest_path", "run_manifest", "metadata")
        if manifest_value is not None:
            manifest_path = Path(manifest_value)
            nested_manifest = _read_json(manifest_path)
    manifest = dict(_read_json(nested_manifest)) if nested_manifest is not None else {}
    # Explicit fields win over nested manifest fields.
    merged = dict(manifest)
    merged.update({key: value for key, value in spec.items() if key != "manifest"})
    bbv_value = _first(
        merged,
        "bbv_path",
        "bbv",
        "simpoint_bbv",
        "profile",
        "profile_path",
    )
    inline_value = _first(merged, "events", "bbv_rows", "rows", "vectors")
    inline_events: tuple[tuple[int, ...], ...] | None = None
    if bbv_value is None and inline_value is None:
        raise ValueError("run specification requires bbv_path or inline events")
    if bbv_value is None:
        bbv_path = Path("<inline-bbv>")
        rows = inline_value if isinstance(inline_value, Sequence) and not isinstance(inline_value, (str, bytes)) else []
        normalized_rows: list[tuple[int, ...]] = []
        for row in rows:
            if isinstance(row, Mapping):
                row = _first(row, "counts", "vector", "values", default=[])
            normalized_rows.append(tuple(int(value) for value in row))
        inline_events = tuple(normalized_rows)
    else:
        bbv_path = Path(bbv_value)
        if not bbv_path.is_file():
            raise FileNotFoundError(bbv_path)
    build_id_value = _first(merged, "build_id", "build", "id", "name")
    build_id = str(build_id_value) if build_id_value is not None else bbv_path.stem
    interval = _first(
        merged,
        "interval_instructions",
        "interval",
        "cpt_interval",
        default=default_interval or 20_000_000,
    )
    interval = int(interval)
    if interval <= 0:
        raise ValueError("interval_instructions must be positive")
    total_value = _first(merged, "total_instructions", "insts", "instruction_total")
    total = int(total_value) if total_value is not None else None
    domain = str(_first(merged, "icount_domain", default="workload_relative_instructions"))
    interval_index_base = int(_first(merged, "interval_index_base", default=0))
    if interval_index_base != 0:
        raise ValueError("only zero-based interval indexes are supported")
    run_workload = _workload_id(_first(merged, "workload_id", "workload", "name"))
    # Keep a stable identity subset for compatibility checks.  Paths and BBV
    # locations are intentionally excluded: the same input may live elsewhere.
    input_fields = {}
    for key in (
        "input_hash",
        "input_sha256",
        "input_fingerprint",
        "args",
        "parameters",
        "env",
        "functional_path",
        "thread_count",
        "threads",
        "seed",
        "icount_domain",
    ):
        if key in merged:
            input_fields[key] = merged[key]
    input_fp = _first(merged, "input_fingerprint", "input_hash", "input_sha256")
    explicit_manifest_hash = _first(merged, "manifest_hash", "run_manifest_hash")
    artifact_sha256 = _first(merged, "artifact_sha256", "elf_sha256", "elf_hash")
    build_identity = _first(merged, "build_identity", "elf_build_id", "build_id")
    return RunSpec(
        build_id=build_id,
        run_id=(str(_first(merged, "run_id")) if _first(merged, "run_id") is not None else None),
        bbv_path=bbv_path,
        manifest=merged,
        manifest_hash=(str(explicit_manifest_hash) if explicit_manifest_hash is not None
                       else _manifest_hash(merged, manifest_path)),
        interval_instructions=interval,
        icount_domain=domain,
        interval_index_base=interval_index_base,
        input_fingerprint=str(input_fp) if input_fp is not None else (_hash_json(input_fields) if input_fields else None),
        workload_id=run_workload,
        total_instructions=total,
        trace_bytes=bbv_path.stat().st_size if bbv_path.is_file() else 0,
        inline_events=inline_events,
        artifact_sha256=str(artifact_sha256) if artifact_sha256 is not None else None,
        build_identity=str(build_identity) if build_identity is not None else None,
    )


def _parse_bbv_line(line: str) -> tuple[int, ...]:
    counts: list[int] = []
    for token in line.split():
        fields = token.split(":")
        if len(fields) < 3:
            continue
        try:
            value = int(fields[-1])
        except ValueError:
            continue
        if value < 0:
            continue
        counts.append(value)
    return tuple(counts)


def _read_events(run: RunSpec, policy: AlignmentPolicy) -> tuple[tuple[BbvEvent, ...], dict[str, Any]]:
    """Read sparse profile rows while accounting for resource budgets."""

    started = time.monotonic()
    if run.inline_events is not None:
        limit = len(run.inline_events)
        truncated_reason = None
        estimated_memory = 0
        if policy.max_events is not None and limit > policy.max_events:
            limit = policy.max_events
            truncated_reason = "max_events"
        if policy.max_memory_bytes is not None:
            while limit and estimated_memory + sum(len(row) * 8 + 32 for row in run.inline_events[:limit]) > policy.max_memory_bytes:
                limit -= 1
                truncated_reason = "max_memory_bytes"
            estimated_memory = sum(len(row) * 8 + 32 for row in run.inline_events[:limit])
        events = tuple(
            BbvEvent(index=index, workload_icount=index * run.interval_instructions, counts=counts)
            for index, counts in enumerate(run.inline_events[:limit])
        )
        return events, {
            "event_count": len(events),
            "trace_bytes": run.trace_bytes,
            "estimated_memory_bytes": estimated_memory,
            "truncated": truncated_reason is not None,
            "truncation_reason": truncated_reason,
            "elapsed_seconds": time.monotonic() - started,
        }
    events: list[BbvEvent] = []
    estimated_memory = 0
    truncated_reason: str | None = None
    opener = gzip.open if run.bbv_path.suffix.endswith("gz") else open
    # ``gzip.open`` accepts text mode and ``open`` does too; type checkers do
    # not understand the common callable, hence the deliberately small local
    # branch.
    with opener(run.bbv_path, "rt", encoding="utf-8") as handle:  # type: ignore[arg-type]
        for index, line in enumerate(handle):
            if policy.max_seconds is not None and time.monotonic() - started > policy.max_seconds:
                truncated_reason = "max_seconds"
                break
            if policy.max_events is not None and len(events) >= policy.max_events:
                truncated_reason = "max_events"
                break
            counts = _parse_bbv_line(line)
            row_memory = len(counts) * 8 + 32
            if policy.max_memory_bytes is not None and estimated_memory + row_memory > policy.max_memory_bytes:
                truncated_reason = "max_memory_bytes"
                break
            events.append(BbvEvent(index=index, workload_icount=index * run.interval_instructions, counts=counts))
            estimated_memory += row_memory
    return tuple(events), {
        "event_count": len(events),
        "trace_bytes": run.trace_bytes,
        "estimated_memory_bytes": estimated_memory,
        "truncated": truncated_reason is not None,
        "truncation_reason": truncated_reason,
        "elapsed_seconds": time.monotonic() - started,
    }


def build_index(
    runs: Iterable[Any] | Mapping[str, Any],
    workload: Any = None,
    policy_version: str | None = POLICY_VERSION,
    *,
    policy: AlignmentPolicy | Mapping[str, Any] | None = None,
) -> PositionIndex:
    """Build reusable sparse BBV indexes for one workload's runs.

    ``runs`` may be a sequence of dictionaries or a mapping keyed by build ID.
    A dictionary minimally needs ``build_id`` and ``bbv_path``; ``manifest``
    and interval metadata are optional.  No weights are read.
    """

    selected_policy = _normalise_policy(policy, policy_version)
    if isinstance(runs, Mapping):
        specs = []
        for build_id, value in runs.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("build_id", build_id)
            elif isinstance(value, RunSpec) or is_dataclass(value) or hasattr(value, "__dict__"):
                item = dict(vars(value)) if hasattr(value, "__dict__") else asdict(value)
                item.setdefault("build_id", build_id)
            else:
                item = {"build_id": build_id, "bbv_path": value}
            specs.append(item)
    else:
        specs = list(runs)
    if not specs:
        raise ValueError("runs must not be empty")

    workload_id = _workload_id(workload)
    workload_interval = None
    if isinstance(workload, Mapping):
        interval_value = _first(workload, "interval_instructions", "interval", "cpt_interval")
        if interval_value is not None:
            workload_interval = int(interval_value)
    workload_manifest_hash = None
    if isinstance(workload, (Mapping, str, Path)):
        if isinstance(workload, Mapping):
            explicit_workload_hash = _first(workload, "manifest_hash", "suite_manifest_sha256")
            workload_manifest_hash = (
                str(explicit_workload_hash)
                if explicit_workload_hash is not None
                else _hash_json(workload)
            )
        elif Path(workload).is_file():
            workload_manifest_hash = _sha256_file(Path(workload))

    normalised: list[RunSpec] = []
    for spec in specs:
        run = _normalise_run(spec, workload_interval)
        if workload_id is not None and run.workload_id is not None and run.workload_id != workload_id:
            # Keep the index constructible; align() returns the typed batch
            # rejection after inspecting all target builds.
            pass
        normalised.append(run)
    if len({run.build_id for run in normalised}) != len(normalised):
        raise ValueError("run build_id values must be unique")

    events: dict[str, tuple[BbvEvent, ...]] = {}
    budget_rows: dict[str, Any] = {
        "max_events": selected_policy.max_events,
        "max_trace_bytes": selected_policy.max_trace_bytes,
        "max_memory_bytes": selected_policy.max_memory_bytes,
        "max_seconds": selected_policy.max_seconds,
        "runs": {},
        "truncated": False,
        "truncation_reasons": [],
    }
    for run in normalised:
        # ``max_trace_bytes`` is a per-build cap.  Recording the offending
        # build separately is important: a source truncation and a target
        # truncation have different remediation paths for a caller.
        if selected_policy.max_trace_bytes is not None and run.trace_bytes > selected_policy.max_trace_bytes:
            budget_rows["truncated"] = True
            budget_rows["truncation_reasons"].append(f"{run.build_id}:max_trace_bytes")
            events[run.build_id] = tuple()
            budget_rows["runs"][run.build_id] = {
                "event_count": 0,
                "trace_bytes": run.trace_bytes,
                "truncated": True,
                "truncation_reason": "max_trace_bytes",
            }
            continue
        loaded, metadata = _read_events(run, selected_policy)
        events[run.build_id] = loaded
        budget_rows["runs"][run.build_id] = metadata
        if metadata["truncated"]:
            budget_rows["truncated"] = True
            budget_rows["truncation_reasons"].append(f"{run.build_id}:{metadata['truncation_reason']}")
    return PositionIndex(
        runs={run.build_id: run for run in normalised},
        events=events,
        policy=selected_policy,
        workload_id=workload_id,
        workload_manifest_hash=workload_manifest_hash,
        budget=budget_rows,
    )


def _vector_overlap(a: Sequence[int], b: Sequence[int]) -> float:
    """Count-multiset overlap used by the legacy locator."""

    denominator = max(len(a), len(b))
    if denominator == 0:
        return 1.0
    return sum((Counter(a) & Counter(b)).values()) / denominator


def _cosine(a: Sequence[int], b: Sequence[int]) -> float:
    if not a or not b:
        return 0.0
    length = max(len(a), len(b))
    aa = list(a) + [0] * (length - len(a))
    bb = list(b) + [0] * (length - len(b))
    norm_a = math.sqrt(sum(value * value for value in aa))
    norm_b = math.sqrt(sum(value * value for value in bb))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0 if aa == bb else 0.0
    return sum(x * y for x, y in zip(aa, bb)) / (norm_a * norm_b)


def _position_value(position: Any) -> tuple[int | None, dict[str, Any]]:
    if isinstance(position, bool):
        raise TypeError("boolean is not a source position")
    if isinstance(position, int):
        return position, {"interval": position}
    if isinstance(position, Mapping):
        value = _first(position, "interval", "point", "index", "source_point_a")
        if value is not None:
            return int(value), dict(position)
        requested = _first(position, "requested_icount", "workload_icount", "icount")
        if requested is not None:
            return None, dict(position) | {"requested_icount": int(requested)}
    # Dataclass/protocol objects commonly expose ``interval`` or ``requested_icount``.
    value = getattr(position, "interval", getattr(position, "point", None))
    if value is not None:
        return int(value), {"interval": int(value)}
    requested = getattr(position, "requested_icount", None)
    if requested is not None:
        return None, {"requested_icount": int(requested)}
    raise TypeError(f"unsupported source position: {position!r}")


def _source_interval(position: Any, run: RunSpec) -> tuple[int | None, dict[str, Any]]:
    interval, metadata = _position_value(position)
    if interval is None:
        requested = metadata.get("requested_icount")
        if requested is None:
            return None, metadata
        interval = requested // run.interval_instructions
    return interval, metadata


def _compatibility_reason(index: PositionIndex, source: RunSpec, target: RunSpec) -> str | None:
    if source.icount_domain != target.icount_domain:
        return INCOMPATIBLE_RUN
    if source.workload_id and target.workload_id and source.workload_id != target.workload_id:
        return INCOMPATIBLE_RUN
    if index.workload_id:
        if source.workload_id and source.workload_id != index.workload_id:
            return INCOMPATIBLE_RUN
        if target.workload_id and target.workload_id != index.workload_id:
            return INCOMPATIBLE_RUN
    # Compare explicit identity fields only.  Missing fields are unknown, not
    # mismatches, which keeps lightweight synthetic fixtures usable.
    for keys in (("input_fingerprint", "input_hash", "input_sha256"), ("args",), ("parameters",), ("env",), ("functional_path",), ("thread_count", "threads"), ("seed",)):
        source_value = _first(source.manifest, *keys)
        target_value = _first(target.manifest, *keys)
        if source_value is not None and target_value is not None and source_value != target_value:
            return INCOMPATIBLE_RUN
    return None


def _next_window(current: int, policy: AlignmentPolicy) -> int:
    if current >= policy.max_window:
        return policy.max_window
    expanded = max(current + 1, int(math.ceil(current * policy.expansion_factor)))
    return min(policy.max_window, expanded)


def _feature_score(
    source_events: Sequence[BbvEvent],
    target_events: Sequence[BbvEvent],
    source_index: int,
    target_index: int,
    center: int,
    radius: int,
    policy: AlignmentPolicy,
) -> dict[str, float | None]:
    source = source_events[source_index]
    target = target_events[target_index]
    local = _vector_overlap(source.counts, target.counts)
    context_values: list[float] = []
    for delta in range(-policy.context_radius, policy.context_radius + 1):
        if delta == 0:
            continue
        source_neighbor = source_index + delta
        target_neighbor = target_index + delta
        if 0 <= source_neighbor < len(source_events) and 0 <= target_neighbor < len(target_events):
            context_values.append(_vector_overlap(source_events[source_neighbor].counts, target_events[target_neighbor].counts))
    context = sum(context_values) / len(context_values) if context_values else local
    distance = abs(target_index - center)
    progress = math.exp(-distance / max(1.0, float(radius)))
    return {
        # BBV-only adapter has no semantic anchor/call graph evidence.
        "anchor_identity": None,
        "sequence_context": context,
        "local_bbv": local,
        "progress_consistency": progress,
        "call_loop_context": None,
    }


def _weighted_score(features: Mapping[str, float | None], policy: AlignmentPolicy) -> float:
    available = [(name, value, policy.feature_weights.get(name, 0.0))
                 for name, value in features.items() if value is not None and policy.feature_weights.get(name, 0.0) > 0]
    if not available:
        return 0.0
    weight_sum = sum(weight for _, _, weight in available)
    return sum(float(value) * weight for _, value, weight in available) / weight_sum


def _candidate_band(
    source_index: int,
    source_run: RunSpec,
    target_run: RunSpec,
    source_events: Sequence[BbvEvent],
    target_events: Sequence[BbvEvent],
    policy: AlignmentPolicy,
    deadline: float | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    source_total = source_run.total_instructions or (len(source_events) * source_run.interval_instructions)
    target_total = target_run.total_instructions or (len(target_events) * target_run.interval_instructions)
    ratio = target_total / source_total if source_total else 1.0
    center = int(round(source_index * ratio))
    radius = min(policy.initial_window, policy.max_window)
    expansions = 0
    considered = 0
    stable = False
    best_scored: list[dict[str, Any]] = []
    truncation_reason: str | None = None

    while True:
        if deadline is not None and time.monotonic() > deadline:
            return [], {"center": center, "radius": radius, "expansions": expansions, "considered": considered}, SEARCH_TRUNCATED
        low = max(0, center - radius)
        high = min(len(target_events) - 1, center + radius)
        if high < low:
            return [], {"center": center, "radius": radius, "expansions": expansions, "considered": considered}, NO_CANDIDATE
        candidates: list[dict[str, Any]] = []
        for target_index in range(low, high + 1):
            considered += 1
            if considered > policy.max_candidates_per_position:
                truncation_reason = "max_candidates_per_position"
                break
            features = _feature_score(source_events, target_events, source_index, target_index, center, radius, policy)
            candidates.append({
                "target_index": target_index,
                "target_point_b": target_index,
                "features": features,
                "score": _weighted_score(features, policy),
                "interval_overlap": features["local_bbv"],
                "context_overlap": features["sequence_context"],
                "progress_consistency": features["progress_consistency"],
                "source_nonzero_blocks": len(source_events[source_index].counts),
                "target_nonzero_blocks": len(target_events[target_index].counts),
                "source_vector_sum": sum(source_events[source_index].counts),
                "target_vector_sum": sum(target_events[target_index].counts),
            })
        candidates.sort(key=lambda item: (-float(item["score"]), abs(item["target_index"] - center), item["target_index"]))
        best_scored = candidates[:policy.candidate_top_k]
        if not best_scored:
            return [], {"center": center, "radius": radius, "expansions": expansions, "considered": considered}, NO_CANDIDATE
        best = best_scored[0]
        # A window touching the physical beginning/end of a finite trace does
        # not imply an omitted candidate on that side when the progress center
        # is itself at the trace boundary.  Interior edge hits still require
        # expansion and are rejected if they cannot be stabilized.
        physical_boundary = (
            (best["target_index"] == 0 and low == 0 and center <= 0)
            or (best["target_index"] == len(target_events) - 1 and high == len(target_events) - 1 and center >= len(target_events) - 1)
        )
        at_edge = best["target_index"] in (low, high) and not physical_boundary
        if not at_edge:
            stable = True
            break
        if radius >= policy.max_window:
            truncation_reason = "edge_at_max_window"
            break
        next_radius = _next_window(radius, policy)
        if next_radius == radius:
            truncation_reason = "edge_at_resource_limit"
            break
        radius = next_radius
        expansions += 1
    metadata = {
        "center": center,
        "radius": radius,
        "expansions": expansions,
        "considered": considered,
        "stable": stable,
        "ratio": ratio,
        "low": max(0, center - radius),
        "high": min(len(target_events) - 1, center + radius),
    }
    if truncation_reason:
        metadata["truncation_reason"] = truncation_reason
        return best_scored, metadata, SEARCH_TRUNCATED
    return best_scored, metadata, None


def _state_sort_key(state: _PathState) -> tuple[float, tuple[tuple[int, int], ...]]:
    # Higher score first; lexicographically earlier target sequence wins ties.
    # ``None`` is represented by -1 so Python never compares unlike types.
    tie_path = tuple((source, -1 if target is None else target) for source, target in state.mappings)
    return (-state.score, tie_path)


def _keep_top(states: list[_PathState], limit: int = 2) -> list[_PathState]:
    states.sort(key=_state_sort_key)
    unique: list[_PathState] = []
    seen: set[tuple[tuple[int, int | None], ...]] = set()
    for state in states:
        if state.mappings in seen:
            continue
        seen.add(state.mappings)
        unique.append(state)
        if len(unique) >= limit:
            break
    return unique


def _global_paths(
    source_indices: Sequence[int],
    candidate_bands: Sequence[Sequence[Mapping[str, Any]]],
    ratio: float,
    policy: AlignmentPolicy,
    deadline: float | None,
) -> tuple[list[_PathState], str | None]:
    """Compute top-two monotonic paths over sparse candidate bands.

    States are keyed by the last target interval and last mapped source.  The
    latter is part of the state because warp cost depends on the distance from
    the previous mapped source after one or more source gaps.  Keeping two
    paths per state is sufficient for a global top-two result; all target
    transitions enforce ``target_index > last_target``.
    """

    states: dict[tuple[int, int | None], list[_PathState]] = {
        (-1, None): [_PathState(score=0.0, mappings=(), last_target=-1, last_source=None)]
    }
    for source_seq, source_index in enumerate(source_indices):
        if deadline is not None and time.monotonic() > deadline:
            return [], SEARCH_TRUNCATED
        next_states: dict[tuple[int, int | None], list[_PathState]] = {}
        # Source gap: carry every reachable last target and mark this source
        # event unmapped.  The penalty makes gaps possible but not preferred.
        for (last_target, last_source), previous in states.items():
            for state in previous:
                gap = _PathState(
                    score=state.score - policy.source_gap_penalty,
                    mappings=state.mappings + ((source_seq, None),),
                    last_target=last_target,
                    last_source=state.last_source,
                )
                next_states.setdefault((last_target, last_source), []).append(gap)

        # Match transitions.  Sparse bands keep this bounded for long traces.
        for candidate in candidate_bands[source_seq]:
            target_index = int(candidate["target_index"])
            match_score = float(candidate["score"])
            for (last_target, last_source), previous in states.items():
                if last_target >= target_index:
                    continue
                for state in previous:
                    target_gap = 0 if last_target < 0 else max(0, target_index - last_target - 1)
                    penalty = policy.target_gap_penalty * target_gap
                    if state.last_source is None:
                        warp = 0.0
                    else:
                        expected = max(1.0, (source_index - state.last_source) * ratio)
                        observed = target_index - last_target
                        warp = policy.warp_penalty * abs(observed - expected) / expected
                    matched = _PathState(
                        score=state.score + match_score - penalty - warp,
                        mappings=state.mappings + ((source_seq, target_index),),
                        last_target=target_index,
                        last_source=source_index,
                    )
                    next_states.setdefault((target_index, source_index), []).append(matched)
        states = {key: _keep_top(value) for key, value in next_states.items()}
        if not states:
            return [], NO_CANDIDATE
    complete: list[_PathState] = []
    for value in states.values():
        complete.extend(value)
    return _keep_top(complete), None


def _path_margin(paths: Sequence[_PathState]) -> float | None:
    if len(paths) < 2:
        return None
    return paths[0].score - paths[1].score


def _result_position(
    source_run: RunSpec,
    target_run: RunSpec,
    source_interval: int,
    source_metadata: Mapping[str, Any],
    target_interval: int | None,
    candidate: Mapping[str, Any] | None,
    source_event_count: int,
    target_event_count: int,
) -> dict[str, Any]:
    requested_icount = source_metadata.get("requested_icount", source_interval * source_run.interval_instructions)
    source_phase = str(source_metadata.get("event_phase", "before_instruction"))
    source_workload_icount = int(source_metadata.get("workload_icount", requested_icount))
    if target_interval is None:
        return {
            "source": {
                "schema_version": SCHEMA_VERSION,
                "build_id": source_run.build_id,
                "build_identity": source_run.build_identity,
                "artifact_sha256": source_run.artifact_sha256,
                "run_id": source_metadata.get("run_id"),
                "anchor_id": source_metadata.get("anchor_id"),
                "occurrence": source_metadata.get("occurrence", source_interval),
                "event_phase": source_phase,
                "interval": source_interval,
                "requested_icount": requested_icount,
                "workload_icount": source_workload_icount,
                "offset_in_interval": source_workload_icount % source_run.interval_instructions,
                "snap_delta_instructions": int(source_metadata.get("snap_delta_instructions", 0)),
            },
            "target": None,
            "status": "rejected",
            "reason": NO_CANDIDATE,
            "anchor_confidence": "R",
            "position_fidelity": "rejected",
            "validation_state": "candidate",
        }
    target_icount = target_interval * target_run.interval_instructions
    features = dict(candidate.get("features", {})) if candidate else {}
    snap_delta = target_interval - int(round(source_interval * (target_run.total_instructions or target_event_count * target_run.interval_instructions) / max(1, source_run.total_instructions or source_event_count * source_run.interval_instructions)))
    return {
        "source": {
            "schema_version": SCHEMA_VERSION,
                "build_id": source_run.build_id,
                "build_identity": source_run.build_identity,
                "artifact_sha256": source_run.artifact_sha256,
                "run_id": source_metadata.get("run_id"),
            "anchor_id": source_metadata.get("anchor_id"),
            "occurrence": source_metadata.get("occurrence", source_interval),
            "event_phase": source_phase,
            "interval": source_interval,
            "requested_icount": requested_icount,
            "workload_icount": source_workload_icount,
            "offset_in_interval": source_workload_icount % source_run.interval_instructions,
            "snap_delta_instructions": int(source_metadata.get("snap_delta_instructions", 0)),
        },
        "target": {
            "position": {
                "schema_version": SCHEMA_VERSION,
                "anchor_id": None,
                "build_identity": target_run.build_identity,
                "artifact_sha256": target_run.artifact_sha256,
                "occurrence": target_interval,
                "event_phase": source_phase,
                "requested_icount": target_icount,
                "workload_icount": target_icount,
                "interval": target_interval,
                "offset_in_interval": 0,
                "snap_delta_instructions": 0,
                "build_id": target_run.build_id,
                "run_id": target_run.run_id,
            }
        },
        "status": "matched",
        "anchor_confidence": "L",
        # A BBV interval has no semantic marker/occurrence identity.  Even a
        # ratio-center hit is therefore an experimental coarse projection,
        # never the ``exact`` or calibrated ``snapped`` tier.
        "position_fidelity": "interpolated",
        "coarse_position": True,
        "snap_delta_intervals": snap_delta,
        "snap_delta_instructions": snap_delta * target_run.interval_instructions,
        "validation_state": "candidate",
        "evidence": {
            "anchor_identity": features.get("anchor_identity"),
            "sequence_context": features.get("sequence_context"),
            "local_bbv": features.get("local_bbv"),
            "progress_consistency": features.get("progress_consistency"),
            "call_loop_context": features.get("call_loop_context"),
            "score": candidate.get("score") if candidate else None,
            "interval_overlap": candidate.get("interval_overlap") if candidate else None,
            "context_overlap": candidate.get("context_overlap") if candidate else None,
        },
    }


def _candidate_lookup(bands: Sequence[Sequence[Mapping[str, Any]]]) -> dict[tuple[int, int], Mapping[str, Any]]:
    return {(source_seq, int(item["target_index"])): item for source_seq, band in enumerate(bands) for item in band}


def _align_target(
    index: PositionIndex,
    source: RunSpec,
    target: RunSpec,
    source_positions: Sequence[Any],
    deadline: float | None,
) -> dict[str, Any]:
    policy = index.policy
    source_events = index.events.get(source.build_id, ())
    target_events = index.events.get(target.build_id, ())
    base = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": policy.version,
        "workload_id": index.workload_id,
        "source_build": source.build_id,
        "target_build": target.build_id,
        "source_manifest_hash": source.manifest_hash,
        "target_manifest_hash": target.manifest_hash,
        "source_build_identity": source.build_identity,
        "target_build_identity": target.build_identity,
        "source_artifact_sha256": source.artifact_sha256,
        "target_artifact_sha256": target.artifact_sha256,
        "workload_manifest_hash": index.workload_manifest_hash,
        "evidence_schema_version": 1,
        "method": "sparse-global-bbv-dp",
        "evidence_adapter": "BBV",
        "confidence_ceiling": "L",
        "experimental": True,
        "production_eligible": False,
        "weights_used": False,
        # Stable envelope for typed early rejections.  Matching fills these
        # aliases with source positions and correspondence evidence below.
        "source_positions": [],
        "correspondences": [],
        "best_candidates": [],
        "anchor_confidence": "R",
        "position_fidelity": "rejected",
        "validation_state": "candidate",
        "global_path_margin": None,
        "evidence": {},
        "manifest_hashes": {
            "source": source.manifest_hash,
            "target": target.manifest_hash,
            "workload": index.workload_manifest_hash,
        },
        "label": "bbv_experimental",
        "synthetic": False,
        "budget": index.budget,
        "trace": {
            "source_event_count": len(source_events),
            "target_event_count": len(target_events),
            "source_trace_bytes": source.trace_bytes,
            "target_trace_bytes": target.trace_bytes,
            "max_events": policy.max_events,
            "max_trace_bytes": policy.max_trace_bytes,
            "max_memory_bytes": policy.max_memory_bytes,
            "max_seconds": policy.max_seconds,
        },
    }
    budget_triggered_builds = [
        build_id for build_id in (source.build_id, target.build_id)
        if index.budget.get("runs", {}).get(build_id, {}).get("truncated")
    ]
    if budget_triggered_builds:
        base.update({
            "status": "rejected",
            "reason": SEARCH_TRUNCATED,
            "positions": [],
            "budget_triggered_builds": budget_triggered_builds,
        })
        return base
    if not source_events or not target_events:
        truncated_builds = [
            build_id for build_id in (source.build_id, target.build_id)
            if any(reason.startswith(f"{build_id}:") for reason in index.budget.get("truncation_reasons", []))
        ]
        base.update({
            "status": "rejected",
            "reason": SEARCH_TRUNCATED if truncated_builds else NO_CANDIDATE,
            "positions": [],
            "budget_triggered_builds": truncated_builds,
        })
        return base
    if not source_positions:
        base.update({"status": "rejected", "reason": NO_CANDIDATE, "positions": []})
        return base

    source_indices: list[int] = []
    source_metadata: list[dict[str, Any]] = []
    for position in source_positions:
        interval, metadata = _source_interval(position, source)
        if interval is None:
            base.update({"status": "rejected", "reason": OUT_OF_TRACE, "positions": []})
            return base
        source_indices.append(interval)
        source_metadata.append(metadata)
    if source_indices != sorted(source_indices) or len(set(source_indices)) != len(source_indices):
        base.update({"status": "rejected", "reason": INVALID_SOURCE_ORDER, "positions": []})
        return base
    if any(item < 0 or item >= len(source_events) for item in source_indices):
        base.update({"status": "rejected", "reason": OUT_OF_TRACE, "positions": []})
        return base

    candidate_bands: list[list[dict[str, Any]]] = []
    search_metadata: list[dict[str, Any]] = []
    point_reasons: list[str | None] = []
    for source_index in source_indices:
        band, metadata, reason = _candidate_band(
            source_index, source, target, source_events, target_events, policy, deadline,
        )
        candidate_bands.append(band)
        search_metadata.append(metadata)
        point_reasons.append(reason)
        if reason == SEARCH_TRUNCATED and not band:
            base.update({"status": "rejected", "reason": SEARCH_TRUNCATED, "positions": [], "search": search_metadata})
            return base
    ratio = (target.total_instructions or len(target_events) * target.interval_instructions) / max(
        1, source.total_instructions or len(source_events) * source.interval_instructions
    )
    paths, path_reason = _global_paths(source_indices, candidate_bands, ratio, policy, deadline)
    if path_reason:
        base.update({"status": "rejected", "reason": path_reason, "positions": [], "search": search_metadata})
        return base
    if not paths:
        base.update({"status": "rejected", "reason": NO_CANDIDATE, "positions": [], "search": search_metadata})
        return base
    best = paths[0]
    margin = _path_margin(paths)
    lookup = _candidate_lookup(candidate_bands)
    mapping_by_source = {source_seq: target_idx for source_seq, target_idx in best.mappings}
    positions: list[dict[str, Any]] = []
    for source_seq, (source_index, metadata) in enumerate(zip(source_indices, source_metadata)):
        target_index = mapping_by_source.get(source_seq)
        candidate = lookup.get((source_seq, target_index)) if target_index is not None else None
        position_result = _result_position(
            source, target, source_index, metadata, target_index, candidate,
            len(source_events), len(target_events),
        )
        position_result["candidates"] = [
            {
                "target_point_b": int(item["target_index"]),
                "score": item["score"],
                "evidence": dict(item.get("features", {})),
            }
            for item in candidate_bands[source_seq]
        ]
        if target_index is None:
            reason = point_reasons[source_seq] or AMBIGUOUS
            position_result["reason"] = reason
            position_result["status"] = "rejected"
            position_result["anchor_confidence"] = "R"
            position_result["position_fidelity"] = "rejected"
        elif point_reasons[source_seq] == SEARCH_TRUNCATED:
            # A band that only became usable at the resource edge is not a
            # stable formal result, even if DP found a path through it.
            position_result["status"] = "rejected"
            position_result["reason"] = SEARCH_TRUNCATED
            position_result["anchor_confidence"] = "R"
            position_result["position_fidelity"] = "rejected"
        positions.append(position_result)
    # Margin and score gates apply to the whole path.  Preserve per-position
    # candidates/evidence for audit even when the batch is rejected.
    score_ok = all(
        item.get("target") is not None and float(item.get("evidence", {}).get("score") or 0.0) >= policy.min_score
        for item in positions
    )
    margin_ok = margin is None or margin >= policy.min_global_margin
    stable_ok = all(bool(item.get("stable", False)) for item in search_metadata)
    all_mapped = all(item["status"] == "matched" for item in positions)
    status = "matched" if score_ok and margin_ok and stable_ok and all_mapped else "rejected"
    reason = None
    if status == "rejected":
        if not stable_ok or any(item.get("truncation_reason") for item in search_metadata):
            reason = SEARCH_TRUNCATED
        elif not all_mapped:
            reason = next((item.get("reason") for item in positions if item.get("status") == "rejected"), NO_CANDIDATE)
        elif not margin_ok:
            reason = AMBIGUOUS
        elif not score_ok:
            reason = LOW_FIDELITY
    # Strictly verify the invariant even though the DP transition enforces it.
    mapped_targets = [item["target"]["position"]["occurrence"] for item in positions if item.get("target")]
    base["source_gap_count"] = sum(item.get("target") is None for item in positions)
    base["mapped_position_count"] = len(mapped_targets)
    base["target_gap_count"] = sum(
        max(0, later - earlier - 1) for earlier, later in zip(mapped_targets, mapped_targets[1:])
    )
    if not mapped_targets:
        # A source-gap-only path is useful evidence that the candidate graph
        # had no usable match, but it is never a successful alignment.
        status = "rejected"
        reason = NO_CANDIDATE
    if mapped_targets != sorted(set(mapped_targets)):
        status = "rejected"
        reason = CROSSING
    aggregate_anchor_confidence = "L" if status == "matched" else "R"
    aggregate_position_fidelity = "interpolated" if status == "matched" else "rejected"
    aggregate_evidence = {
        "adapter": "BBV",
        "global_path_score": best.score,
        "global_path_margin": margin,
        "source_gap_count": base["source_gap_count"],
        "target_gap_count": base["target_gap_count"],
        "coarse_only": True,
    }
    base.update({
        "status": status,
        "reason": reason,
        # M0 protocol aliases.  ``positions`` remains the concise M1 name;
        # ``correspondences`` makes this result consumable by protocol tools.
        "source_positions": [item["source"] for item in positions],
        "correspondences": positions,
        "best_candidates": [item.get("candidates", []) for item in positions],
        "anchor_confidence": aggregate_anchor_confidence,
        "position_fidelity": aggregate_position_fidelity,
        "validation_state": "candidate",
        "label": "bbv_experimental",
        "synthetic": False,
        "manifest_hashes": {
            "source": source.manifest_hash,
            "target": target.manifest_hash,
            "workload": index.workload_manifest_hash,
        },
        "evidence": aggregate_evidence,
        "global_path_score": best.score,
        "global_path_margin": margin,
        "top_paths": [
            {"score": path.score, "mappings": [{"source_sequence": i, "target_interval": j} for i, j in path.mappings]}
            for path in paths
        ],
        "search": search_metadata,
        "positions": positions,
    })
    return base


def align(
    index: PositionIndex,
    source_build: str,
    target_builds: Sequence[str] | str,
    source_positions: Sequence[Any],
    *,
    policy_version: str | None = None,
) -> dict[str, Any]:
    """Align source positions into one or more target builds.

    The returned batch always contains one typed result per target.  A batch
    compatibility failure is represented in the target result and never
    fabricates a B position.
    """

    if policy_version is not None and policy_version != index.policy.version:
        raise ValueError("policy_version does not match the index policy")
    targets = [target_builds] if isinstance(target_builds, str) else list(target_builds)
    if source_build not in index.runs:
        raise KeyError(f"unknown source build: {source_build}")
    source = index.runs[source_build]
    started = time.monotonic()
    deadline = started + index.policy.max_seconds if index.policy.max_seconds is not None else None
    target_results: dict[str, dict[str, Any]] = {}
    for target_build in targets:
        if target_build not in index.runs:
            target_results[target_build] = {
                "schema_version": SCHEMA_VERSION,
                "source_build": source_build,
                "target_build": target_build,
                "status": "rejected",
                "reason": INCOMPATIBLE_RUN,
                "source_positions": [],
                "correspondences": [],
                "best_candidates": [],
                "anchor_confidence": "R",
                "position_fidelity": "rejected",
                "validation_state": "candidate",
                "global_path_margin": None,
                "evidence": {},
                "manifest_hashes": {"source": source.manifest_hash, "target": None},
                "label": "bbv_experimental",
                "synthetic": False,
                "positions": [],
            }
            continue
        target = index.runs[target_build]
        compatibility = _compatibility_reason(index, source, target)
        if compatibility:
            target_results[target_build] = {
                "schema_version": SCHEMA_VERSION,
                "policy_version": index.policy.version,
                "source_build": source_build,
                "target_build": target_build,
                "source_manifest_hash": source.manifest_hash,
                "target_manifest_hash": target.manifest_hash,
                "status": "rejected",
                "reason": compatibility,
                "source_positions": [],
                "correspondences": [],
                "best_candidates": [],
                "anchor_confidence": "R",
                "position_fidelity": "rejected",
                "validation_state": "candidate",
                "global_path_margin": None,
                "evidence": {},
                "manifest_hashes": {
                    "source": source.manifest_hash,
                    "target": target.manifest_hash,
                    "workload": index.workload_manifest_hash,
                },
                "label": "bbv_experimental",
                "synthetic": False,
                "positions": [],
                "weights_used": False,
            }
            continue
        target_results[target_build] = _align_target(index, source, target, source_positions, deadline)
    elapsed = time.monotonic() - started
    batch_status = "matched" if target_results and all(item.get("status") == "matched" for item in target_results.values()) else "rejected"
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": index.policy.version,
        "workload_id": index.workload_id,
        "source_build": source_build,
        "target_builds": targets,
        "status": batch_status,
        "elapsed_seconds": elapsed,
        "weights_used": False,
        "results": target_results,
        # ``targets`` is a concise alias useful to callers; both values refer
        # to the same serializable per-build result data.
        "targets": target_results,
    }


class PositionAligner:
    """Small stateful facade around :func:`build_index` and :func:`align`."""

    def __init__(self, policy: AlignmentPolicy | Mapping[str, Any] | None = None,
                 policy_version: str | None = None) -> None:
        self.policy = _normalise_policy(policy, policy_version)

    def build_index(self, runs: Iterable[Any] | Mapping[str, Any], workload: Any = None) -> PositionIndex:
        return build_index(runs, workload, policy=self.policy)

    def align(self, index: PositionIndex, source_build: str,
              target_builds: Sequence[str] | str, source_positions: Sequence[Any]) -> dict[str, Any]:
        return align(index, source_build, target_builds, source_positions, policy_version=self.policy.version)


__all__ = [
    "AlignmentPolicy",
    "BbvEvent",
    "RunSpec",
    "PositionIndex",
    "PositionAligner",
    "build_index",
    "align",
    "NO_ANCHOR",
    "NO_CANDIDATE",
    "SEARCH_TRUNCATED",
    "AMBIGUOUS",
    "CROSSING",
    "MARKER_GAP_TOO_LARGE",
    "LOW_FIDELITY",
    "OUT_OF_TRACE",
    "POST_ALIGN_DIVERGENCE",
    "INVALID_SOURCE_ORDER",
    "INCOMPATIBLE_RUN",
    "NONDETERMINISTIC_TRACE",
    "ARTIFACT_MISMATCH",
    "EVIDENCE_COLLECTION_FAILED",
]
