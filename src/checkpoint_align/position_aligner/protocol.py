"""Versioned runtime protocol for dynamic cross-ELF progress alignment.

The protocol is deliberately independent of QEMU/NEMU.  Runners produce
``BuildRun`` and ``ProgressEvent`` records; the aligner consumes those records
and returns an explicit correspondence with independent validation slots.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
EVENT_PHASES = {"before_instruction"}
POSITION_STATUSES = {"exact", "snapped", "ambiguous", "no_correspondence"}
VALIDATION_STATUSES = {"candidate", "validated", "failed", "not_run"}


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 hex string") from exc
    return value.lower()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class BuildRun:
    workload_id: str
    build_id: str
    run_id: str
    input_id: str
    functional_path_id: str
    icount_domain: str
    interval_instructions: int
    interval_index_base: int
    event_phase: str
    elf_sha256: str
    manifest_sha256: str
    terminal_marker: str
    deterministic: bool
    compiler: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    arguments: tuple[str, ...] = ()
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    tools: Mapping[str, Any] = field(default_factory=dict)
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    exit_status: int | None = 0
    terminal_observed: bool | None = None
    event_trace_sha256: str | None = None
    elf_type: str = "ET_EXEC"
    thread_count: int = 1

    def __post_init__(self) -> None:
        for name in ("workload_id", "build_id", "run_id", "input_id", "functional_path_id", "icount_domain", "event_phase", "terminal_marker"):
            _text(getattr(self, name), name)
        if self.icount_domain != "workload_relative_instructions":
            raise ValueError("icount_domain must be workload_relative_instructions")
        if self.interval_instructions <= 0 or self.interval_index_base != 0:
            raise ValueError("interval must be positive and interval_index_base must be zero")
        if self.event_phase not in EVENT_PHASES:
            raise ValueError(f"unknown event phase: {self.event_phase}")
        _sha(self.elf_sha256, "elf_sha256")
        _sha(self.manifest_sha256, "manifest_sha256")
        if self.stdout_sha256 is not None:
            _sha(self.stdout_sha256, "stdout_sha256")
        if self.stderr_sha256 is not None:
            _sha(self.stderr_sha256, "stderr_sha256")
        if self.event_trace_sha256 is not None:
            _sha(self.event_trace_sha256, "event_trace_sha256")
        if self.elf_type != "ET_EXEC" or self.thread_count != 1 or not self.deterministic:
            raise ValueError("only deterministic single-thread ET_EXEC runs are supported")

    def to_dict(self) -> dict[str, Any]:
        value = {"schema_version": SCHEMA_VERSION, **self.__dict__}
        value["compiler"] = dict(self.compiler)
        value["environment"] = dict(self.environment)
        value["arguments"] = list(self.arguments)
        value["artifacts"] = dict(self.artifacts)
        value["tools"] = dict(self.tools)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BuildRun":
        data = dict(value)
        version = data.pop("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported BuildRun schema_version: {version}")
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"unknown BuildRun fields: {', '.join(sorted(unknown))}")
        data["arguments"] = tuple(data.get("arguments", ()))
        return cls(**data)


@dataclass(frozen=True)
class ProgressEvent:
    build_id: str
    run_id: str
    anchor_id: str
    semantic_key: str
    occurrence: int
    pc: int
    workload_icount: int | None
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("build_id", "run_id", "anchor_id", "semantic_key"):
            _text(getattr(self, name), name)
        if self.occurrence < 0 or self.pc < 0 or (self.workload_icount is not None and self.workload_icount < 0):
            raise ValueError("occurrence, pc and workload_icount must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **self.__dict__, "context": dict(self.context)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProgressEvent":
        data = dict(value)
        version = data.pop("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported ProgressEvent schema_version: {version}")
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"unknown ProgressEvent fields: {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass(frozen=True)
class Position:
    build_id: str
    anchor_id: str
    occurrence: int
    pc: int
    workload_icount: int | None
    interval: int | None
    offset: int | None
    requested_icount: int | None
    actual_delta_instructions: int | None = None

    def __post_init__(self) -> None:
        for name in ("build_id", "anchor_id"):
            _text(getattr(self, name), name)
        if self.occurrence < 0 or self.pc < 0:
            raise ValueError("position values must be non-negative")
        coordinates = (self.workload_icount, self.interval, self.offset, self.requested_icount, self.actual_delta_instructions)
        if any(value is None for value in coordinates):
            if any(value is not None for value in coordinates):
                raise ValueError("position coordinates must be all present or all absent")
        else:
            if min(value for value in coordinates[:4] if value is not None) < 0:
                raise ValueError("position values must be non-negative")
            if self.actual_delta_instructions != self.workload_icount - self.requested_icount:
                raise ValueError("actual_delta_instructions must be observed-requested")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **self.__dict__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Position":
        data = dict(value)
        version = data.pop("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported Position schema_version: {version}")
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"unknown Position fields: {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass(frozen=True)
class Correspondence:
    source: Position
    target: Position | None
    position_status: str
    restore_status: str = "not_run"
    cross_build_status: str = "not_run"
    coverage_status: str = "not_run"
    evidence: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.position_status not in POSITION_STATUSES:
            raise ValueError(f"unknown position_status: {self.position_status}")
        for name in ("restore_status", "cross_build_status", "coverage_status"):
            if getattr(self, name) not in VALIDATION_STATUSES:
                raise ValueError(f"unknown {name}")
        if self.position_status in {"exact", "snapped"} and self.target is None:
            raise ValueError("matched correspondence requires target")
        if self.position_status in {"ambiguous", "no_correspondence"} and self.target is not None:
            raise ValueError("unresolved correspondence cannot force a target")
        if self.position_status != "exact" and self.position_status != "snapped" and not self.reason:
            raise ValueError("unresolved correspondence requires reason")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "source": self.source.to_dict(), "target": self.target.to_dict() if self.target else None, "position_status": self.position_status, "restore_status": self.restore_status, "cross_build_status": self.cross_build_status, "coverage_status": self.coverage_status, "evidence": dict(self.evidence), "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Correspondence":
        return cls(source=Position.from_dict(value["source"]), target=Position.from_dict(value["target"]) if value.get("target") else None, position_status=value["position_status"], restore_status=value.get("restore_status", "not_run"), cross_build_status=value.get("cross_build_status", "not_run"), coverage_status=value.get("coverage_status", "not_run"), evidence=value.get("evidence", {}), reason=value.get("reason"))


@dataclass(frozen=True)
class AlignmentResult:
    source: Position
    correspondence: Correspondence
    status: str
    global_path_margin: float | None
    top_paths: Sequence[Mapping[str, Any]] = ()
    manifest_hashes: Mapping[str, str] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"matched", "rejected"}:
            raise ValueError("status must be matched or rejected")
        if self.status == "matched" and self.correspondence.target is None:
            raise ValueError("matched result requires target")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "status": self.status, "source": self.source.to_dict(), "correspondence": self.correspondence.to_dict(), "global_path_margin": self.global_path_margin, "top_paths": list(self.top_paths), "manifest_hashes": dict(self.manifest_hashes), "diagnostics": dict(self.diagnostics)}


def validate_run(value: BuildRun | Mapping[str, Any]) -> BuildRun:
    def reject_weights(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if "weight" in str(key).lower():
                    raise ValueError("BuildRun must not contain SimPoint weights")
                reject_weights(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                reject_weights(child)
    reject_weights(value.to_dict() if isinstance(value, BuildRun) else value)
    return value if isinstance(value, BuildRun) else BuildRun.from_dict(value)


def validate_events(value: Sequence[ProgressEvent | Mapping[str, Any]], run: BuildRun | None = None) -> tuple[ProgressEvent, ...]:
    events = tuple(item if isinstance(item, ProgressEvent) else ProgressEvent.from_dict(item) for item in value)
    if run is not None:
        if any(event.build_id != run.build_id or event.run_id != run.run_id for event in events):
            raise ValueError("event does not match BuildRun identity")
        for key in ("occurrence",):
            grouped: dict[str, list[int]] = {}
            for event in events:
                grouped.setdefault(event.anchor_id, []).append(event.occurrence)
            if any(values != list(range(len(values))) for values in grouped.values()):
                raise ValueError(f"{key} must be zero-based and contiguous per anchor")
    return events


def validate_position(value: Position | Mapping[str, Any], run: BuildRun) -> Position:
    position = value if isinstance(value, Position) else Position.from_dict(value)
    if position.build_id != run.build_id:
        raise ValueError("position does not match BuildRun identity")
    if position.workload_icount is not None:
        interval, offset = divmod(position.workload_icount, run.interval_instructions)
        if (position.interval, position.offset) != (interval, offset):
            raise ValueError("position interval/offset projection mismatch")
    return position
