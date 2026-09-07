#!/usr/bin/env python3
"""DWARF/source-anchor indexing and occurrence alignment primitives.

This module is deliberately independent of the legacy checkpoint experiment.
It turns static information in an ELF into a build-bound catalog and provides
small, deterministic helpers for converting a PC trace into sparse semantic
events and aligning two such event streams.  It does not run an emulator and
does not read SimPoint weights.

The collector accepts an instruction-PC trace (JSON, JSONL, or a whitespace
list) as an input.  A trace containing only sampled PCs cannot prove that an
entry was executed; the resulting events are therefore labelled as observed
evidence and retain the unmatched sample count.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


SCHEMA_VERSION = 1
COLLECTOR_NAME = "dwarf_source"
COLLECTOR_VERSION = "0.1"
DEFAULT_MAX_LINE_ANCHORS = 100_000
DEFAULT_MAX_ALIGNMENT_CELLS = 2_000_000


class DwarfSourceError(RuntimeError):
    """Raised when an ELF/catalog input cannot be interpreted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_hash(value: Any) -> str:
    """Hash JSON using a stable representation suitable for manifests."""

    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        value = json.loads(path.read_text(encoding="utf-8"))
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_sha256(value: Any) -> str:
    """Return the provenance hash for a manifest path or in-memory mapping.

    File inputs use the exact artifact bytes so they bind to the same hash
    recorded by the prepared-suite and PositionAligner reports.  Mappings use
    the canonical JSON representation because no source byte stream exists.
    """

    if isinstance(value, (str, os.PathLike)):
        return sha256_file(Path(value))
    return canonical_json_hash(value)


def parse_path_remap(value: str) -> tuple[str, str]:
    """Parse ``OLD=NEW`` path-remap syntax used by the CLI."""

    old, separator, new = value.partition("=")
    if not separator or not old:
        raise ValueError(f"path remap must be OLD=NEW: {value!r}")
    return old.rstrip("/"), new.rstrip("/")


def canonical_source_path(
    raw_path: str | None,
    *,
    comp_dir: str | None = None,
    path_remaps: Sequence[tuple[str, str]] = (),
) -> str | None:
    """Return a checkout-independent source path.

    Explicit remaps are authoritative.  Without one, common source-root
    suffixes (``src/``, ``include/`` and ``spec2006/``) are retained so the
    current profile artifacts from different checkouts still compare.  An
    unresolved absolute path is represented by an ``external/<digest>/``
    namespace instead of leaking a machine-specific checkout path.
    """

    if raw_path is None:
        return None
    raw = str(raw_path).strip().replace("\\", "/")
    if not raw or raw in {"?", "??", "<unknown>"}:
        return None

    # DWARF paths are often relative to the compilation directory.
    if not raw.startswith("/") and comp_dir:
        raw = posixpath.join(str(comp_dir).replace("\\", "/"), raw)
    raw = posixpath.normpath(raw)

    for old, new in path_remaps:
        old_norm = posixpath.normpath(old.replace("\\", "/"))
        if raw == old_norm or raw.startswith(old_norm + "/"):
            suffix = raw[len(old_norm) :].lstrip("/")
            return posixpath.normpath(posixpath.join(new, suffix)) if suffix else (new or ".")

    if not raw.startswith("/"):
        return raw.lstrip("./") or "."

    components = [part for part in raw.split("/") if part]
    # Keep a stable repository-relative suffix where possible.  The first
    # matching marker is intentionally conservative and deterministic.
    for marker in ("spec2006", "spec2017", "workloads", "src", "include"):
        if marker in components:
            index = len(components) - 1 - components[::-1].index(marker)
            suffix = "/".join(components[index:])
            if marker in {"src", "include"}:
                suffix = "/".join(components[index:])
            return suffix

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"external/{digest}/{components[-1] if components else 'unknown'}"


def _which(candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        if os.path.isabs(candidate) and Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_tool(command: Sequence[str], *, timeout: int = 180) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DwarfSourceError(f"tool failed: {' '.join(command)}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = detail[-1] if detail else f"exit {completed.returncode}"
        raise DwarfSourceError(f"tool failed: {' '.join(command)}: {suffix}")
    return completed.stdout, completed.stderr


def _tool_version(tool: str) -> str | None:
    try:
        output, _ = _run_tool((tool, "--version"), timeout=15)
    except DwarfSourceError:
        return None
    first = output.strip().splitlines()
    return first[0] if first else None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"0x([0-9a-fA-F]+)", value)
    if match:
        return int(match.group(1), 16)
    match = re.search(r"(?<![A-Za-z])[-+]?\d+(?![A-Za-z])", value)
    return int(match.group(0), 10) if match else None


def _parse_quoted(value: str) -> str | None:
    match = re.search(r'"((?:\\.|[^"\\])*)"', value)
    if not match:
        return None
    # JSON decoding handles the common DWARF escaped characters.  Keep the
    # original content when a producer emits a non-JSON escape.
    try:
        return json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return match.group(1).replace('\\"', '"').replace("\\\\", "\\")


@dataclass(frozen=True)
class SourceLocation:
    path: str | None = None
    line: int | None = None
    column: int | None = None
    discriminator: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "discriminator": self.discriminator,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "SourceLocation | None":
        if not value:
            return None
        return cls(
            path=value.get("path"),
            line=value.get("line"),
            column=value.get("column"),
            discriminator=value.get("discriminator"),
        )


@dataclass(frozen=True)
class AddressRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"invalid address range [{self.start}, {self.end})")

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class Anchor:
    anchor_id: str
    kind: str
    name: str | None
    linkage_name: str | None
    source: SourceLocation | None
    ranges: tuple[AddressRange, ...]
    confidence: str
    origin: str
    parent_anchor_id: str | None = None
    inline_chain: tuple[str, ...] = ()
    image_relative_pc: int | None = None
    die_offset: int | None = None
    event_kind: str | None = None
    recovery: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "kind": self.kind,
            "name": self.name,
            "linkage_name": self.linkage_name,
            "source": self.source.to_dict() if self.source else None,
            "ranges": [item.to_dict() for item in self.ranges],
            "confidence": self.confidence,
            "origin": self.origin,
            "parent_anchor_id": self.parent_anchor_id,
            "inline_chain": list(self.inline_chain),
            "image_relative_pc": self.image_relative_pc,
            "die_offset": self.die_offset,
            "event_kind": self.event_kind,
            "recovery": self.recovery,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Anchor":
        return cls(
            anchor_id=str(value["anchor_id"]),
            kind=str(value["kind"]),
            name=value.get("name"),
            linkage_name=value.get("linkage_name"),
            source=SourceLocation.from_dict(value.get("source")),
            ranges=tuple(AddressRange(int(r["start"]), int(r["end"])) for r in value.get("ranges", [])),
            confidence=str(value.get("confidence", "L")),
            origin=str(value.get("origin", "unknown")),
            parent_anchor_id=value.get("parent_anchor_id"),
            inline_chain=tuple(value.get("inline_chain", [])),
            image_relative_pc=value.get("image_relative_pc"),
            die_offset=value.get("die_offset"),
            event_kind=value.get("event_kind"),
            recovery=value.get("recovery"),
        )


@dataclass(frozen=True)
class PcMapping:
    start: int
    end: int
    anchor_ids: tuple[str, ...] = ()
    function_anchor_id: str | None = None
    source: SourceLocation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "anchor_ids": list(self.anchor_ids),
            "function_anchor_id": self.function_anchor_id,
            "source": self.source.to_dict() if self.source else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PcMapping":
        return cls(
            start=int(value["start"]),
            end=int(value["end"]),
            anchor_ids=tuple(value.get("anchor_ids", [])),
            function_anchor_id=value.get("function_anchor_id"),
            source=SourceLocation.from_dict(value.get("source")),
        )


@dataclass
class AnchorCatalog:
    """Static, build-bound anchor index for one ELF."""

    elf_path: str
    artifact_sha256: str
    build_id: str | None
    build_identity: str
    manifest_hash: str | None
    architecture: str | None
    text_base: int | None
    text_size: int | None
    anchors: tuple[Anchor, ...]
    pc_map: tuple[PcMapping, ...]
    line_entries: tuple[dict[str, Any], ...]
    source_files: tuple[str, ...]
    capabilities: dict[str, Any]
    provenance: dict[str, Any]
    path_policy: dict[str, Any]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "collector": {"name": COLLECTOR_NAME, "version": COLLECTOR_VERSION},
            "elf": {
                "path": self.elf_path,
                "sha256": self.artifact_sha256,
                "build_id": self.build_id,
                "build_identity": self.build_identity,
                "architecture": self.architecture,
                "text_base": self.text_base,
                "text_size": self.text_size,
            },
            "manifest_hash": self.manifest_hash,
            "path_policy": self.path_policy,
            "capabilities": self.capabilities,
            "source_files": list(self.source_files),
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "pc_map": [mapping.to_dict() for mapping in self.pc_map],
            "line_entries": list(self.line_entries),
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnchorCatalog":
        elf = value.get("elf", {})
        return cls(
            elf_path=str(elf.get("path", "")),
            artifact_sha256=str(elf.get("sha256", "")),
            build_id=elf.get("build_id"),
            build_identity=str(elf.get("build_identity", elf.get("sha256", ""))),
            manifest_hash=value.get("manifest_hash"),
            architecture=elf.get("architecture"),
            text_base=elf.get("text_base"),
            text_size=elf.get("text_size"),
            anchors=tuple(Anchor.from_dict(item) for item in value.get("anchors", [])),
            pc_map=tuple(PcMapping.from_dict(item) for item in value.get("pc_map", [])),
            line_entries=tuple(value.get("line_entries", [])),
            source_files=tuple(value.get("source_files", [])),
            capabilities=dict(value.get("capabilities", {})),
            provenance=dict(value.get("provenance", {})),
            path_policy=dict(value.get("path_policy", {})),
            schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "AnchorCatalog":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def anchor_by_id(self) -> dict[str, Anchor]:
        return {anchor.anchor_id: anchor for anchor in self.anchors}

    def lookup_pc(self, pc: int) -> tuple[Anchor, ...]:
        """Return anchors covering ``pc``, most specific range first."""

        candidates: list[tuple[int, int, Anchor]] = []
        rank = {"loop": 0, "loop_candidate": 1, "inline": 2, "function": 3, "symbol": 4}
        for anchor in self.anchors:
            for address_range in anchor.ranges:
                if address_range.start <= pc < address_range.end:
                    candidates.append((address_range.end - address_range.start, rank.get(anchor.kind, 5), anchor))
                    break
        candidates.sort(key=lambda item: (item[0], item[1], item[2].anchor_id))
        return tuple(item[2] for item in candidates)

    def function_entries(self) -> dict[int, Anchor]:
        entries: dict[int, Anchor] = {}
        for anchor in self.anchors:
            if anchor.kind not in {"function", "symbol"} or not anchor.ranges:
                continue
            start = min(item.start for item in anchor.ranges)
            # Prefer DWARF over a symbol alias at the same entry.
            old = entries.get(start)
            if old is None or (old.origin != "dwarf" and anchor.origin == "dwarf"):
                entries[start] = anchor
        return entries

    def semantic_anchor(self, anchor_id: str) -> Anchor | None:
        return self.anchor_by_id().get(anchor_id)


@dataclass
class _DwarfDie:
    offset: int
    tag: str
    indent: int
    parent_offset: int | None
    attrs: dict[str, Any] = field(default_factory=dict)


def _parse_attr_value(raw: str) -> Any:
    quoted = _parse_quoted(raw)
    if quoted is not None:
        return quoted
    if "true" in raw.lower() and not re.search(r"[A-Za-z]true", raw, re.I):
        return True
    if "false" in raw.lower() and not re.search(r"[A-Za-z]false", raw, re.I):
        return False
    return _parse_int(raw)


# llvm-dwarfdump puts the DIE nesting indentation *after* the DIE offset
# (``0x123:     DW_TAG_...``), not before it.  Capturing that field is
# essential for reconstructing compile-unit/inline parent chains.
_DIE_RE = re.compile(
    r"^(?P<prefix>\s*)0x(?P<offset>[0-9a-fA-F]+):(?P<indent>\s+)(?P<tag>DW_TAG_[A-Za-z0-9_]+)\s*$"
)
_NULL_RE = re.compile(r"^(?P<prefix>\s*)0x[0-9a-fA-F]+:(?P<indent>\s+)NULL\s*$")
_ATTR_RE = re.compile(r"^\s+DW_AT_(?P<name>[A-Za-z0-9_]+)\s+(?P<value>.*)$")
_RANGE_RE = re.compile(r"\[(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\)")


def parse_llvm_dwarf_info(text: str) -> list[_DwarfDie]:
    """Parse the stable, human-readable subset emitted by llvm-dwarfdump."""

    dies: list[_DwarfDie] = []
    stack: list[_DwarfDie] = []
    pending_ranges: _DwarfDie | None = None
    for line in text.splitlines():
        if pending_ranges is not None:
            for match in _RANGE_RE.finditer(line):
                pending_ranges.attrs.setdefault("ranges", []).append(
                    (int(match.group(1), 16), int(match.group(2), 16))
                )
            if ")" in line and ("DW_OP" not in line):
                pending_ranges = None

        die_match = _DIE_RE.match(line)
        if die_match:
            indent = len(die_match.group("prefix")) + len(die_match.group("indent"))
            while stack and stack[-1].indent >= indent:
                stack.pop()
            parent = stack[-1].offset if stack else None
            die = _DwarfDie(
                offset=int(die_match.group("offset"), 16),
                tag=die_match.group("tag")[len("DW_TAG_") :],
                indent=indent,
                parent_offset=parent,
            )
            dies.append(die)
            stack.append(die)
            pending_ranges = None
            continue

        null_match = _NULL_RE.match(line)
        if null_match:
            indent = len(null_match.group("prefix")) + len(null_match.group("indent"))
            while stack and stack[-1].indent >= indent:
                stack.pop()
            pending_ranges = None
            continue

        attr_match = _ATTR_RE.match(line)
        if attr_match and stack:
            name = attr_match.group("name")
            raw = attr_match.group("value")
            if name == "ranges":
                stack[-1].attrs[name] = []
                pending_ranges = stack[-1]
                for match in _RANGE_RE.finditer(raw):
                    stack[-1].attrs[name].append(
                        (int(match.group(1), 16), int(match.group(2), 16))
                    )
                if ")" in raw:
                    pending_ranges = None
            else:
                stack[-1].attrs[name] = _parse_attr_value(raw)
    return dies


def _die_ranges(die: _DwarfDie) -> tuple[AddressRange, ...]:
    ranges = die.attrs.get("ranges") or []
    result: list[AddressRange] = []
    for start, end in ranges:
        if end > start >= 0:
            result.append(AddressRange(start, end))
    if result:
        return tuple(result)
    low = die.attrs.get("low_pc")
    high = die.attrs.get("high_pc")
    if isinstance(low, int) and isinstance(high, int):
        # DW_AT_high_pc is either an absolute address or an offset from low_pc.
        end = high if high > low else low + high
        if end > low >= 0:
            return (AddressRange(low, end),)
    return ()


def _parse_llvm_line_table(
    text: str,
    *,
    path_remaps: Sequence[tuple[str, str]],
    max_entries: int,
) -> tuple[dict[int, dict[str, Any]], bool]:
    """Parse line rows and return one deterministic row per PC."""

    files: dict[int, str] = {}
    directories: dict[int, str] = {}
    pending_file: int | None = None
    rows: dict[int, dict[str, Any]] = {}
    truncated = False
    row_re = re.compile(
        r"^\s*(0x[0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*(.*)$"
    )
    for line in text.splitlines():
        if line.startswith("debug_line["):
            files = {}
            directories = {}
            pending_file = None
            continue
        directory_match = re.search(r"include_directories\[\s*(\d+)\s*\]\s*=\s+(.+)$", line)
        if directory_match:
            value = _parse_quoted(directory_match.group(2))
            if value is not None:
                directories[int(directory_match.group(1))] = value
            continue
        file_header = re.search(r"file_names\[\s*(\d+)\]:", line)
        if file_header:
            pending_file = int(file_header.group(1))
            continue
        if pending_file is not None and re.search(r"\bname:\s+", line):
            value_match = re.search(r"\bname:\s+(.+)$", line)
            value = _parse_quoted(value_match.group(1)) if value_match else None
            if value is not None:
                files[pending_file] = value
            continue
        if pending_file is not None and re.search(r"\bdir_index:\s+", line):
            # Keep the directory prefix in the table so relative names are
            # canonicalized consistently with absolute names.
            index_match = re.search(r"\bdir_index:\s+(\d+)", line)
            if index_match and pending_file in files:
                directory = directories.get(int(index_match.group(1)))
                if directory and not files[pending_file].startswith("/"):
                    files[pending_file] = posixpath.join(directory, files[pending_file])
            pending_file = None
            continue
        row_match = row_re.match(line)
        if not row_match:
            continue
        pc = int(row_match.group(1), 16)
        line_number = int(row_match.group(2))
        column = int(row_match.group(3))
        file_index = int(row_match.group(4))
        discriminator = int(row_match.group(6))
        flags = row_match.group(8)
        if "end_sequence" in flags:
            continue
        path = canonical_source_path(files.get(file_index), path_remaps=path_remaps)
        if len(rows) >= max_entries and pc not in rows:
            truncated = True
            continue
        candidate = {
            "pc": pc,
            "source": SourceLocation(path, line_number, column, discriminator).to_dict(),
            "file_index": file_index,
        }
        # Multiple rows can describe one instruction.  The first row is the
        # producer's deterministic choice; discriminator/column remain in it.
        rows.setdefault(pc, candidate)
    return rows, truncated


def _parse_elf_sections(text: str) -> tuple[str | None, int | None, int | None, dict[str, tuple[int, int]]]:
    machine = None
    machine_match = re.search(r"Machine:\s+(.+)$", text, re.M)
    if machine_match:
        machine = machine_match.group(1).strip()
    sections: dict[str, tuple[int, int]] = {}
    # readelf -SW emits one row with Name, Address, Off, Size columns.
    section_re = re.compile(
        r"^\s*\[\s*\d+\]\s+(\S*)\s+\S+\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+",
        re.M,
    )
    for match in section_re.finditer(text):
        name = match.group(1)
        if name:
            sections[name] = (int(match.group(2), 16), int(match.group(4), 16))
    text_base, text_size = sections.get(".text", (None, None))
    return machine, text_base, text_size, sections


def _parse_build_id(text: str) -> str | None:
    match = re.search(r"Build ID:\s*([0-9a-fA-F]+)", text)
    return match.group(1).lower() if match else None


def _parse_symbols(
    text: str,
    *,
    text_base: int | None,
    text_size: int | None,
    path_remaps: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    # Avoid accidentally interpreting the section-header table as symbols.
    for line in text.splitlines():
        if not re.match(r"^\s*\d+:\s+", line):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            value = int(parts[1], 16)
            size = int(parts[2], 10)
        except ValueError:
            continue
        symbol_type = parts[3]
        section = parts[6]
        name = parts[7].strip()
        if symbol_type == "FILE":
            # FILE symbols delimit object-file contributions, but they do not
            # reliably identify an LTO-produced function.  Do not attach a
            # stale filename to a symbol-only anchor; DWARF is the source of
            # truth for source paths.
            continue
        if symbol_type != "FUNC" or section == "UND" or value == 0:
            continue
        if text_base is not None and text_size is not None:
            if value < text_base or value >= text_base + text_size:
                continue
        records.append(
            {
                "value": value,
                "size": size,
                "name": name or None,
                "source_unit": None,
                "section": section,
            }
        )
    records.sort(key=lambda item: (item["value"], item["name"] or ""))
    # Resolve zero-sized symbols to the next function/text end.
    for index, record in enumerate(records):
        if record["size"]:
            record["end"] = record["value"] + record["size"]
            continue
        next_value = next(
            (other["value"] for other in records[index + 1 :] if other["value"] > record["value"]),
            None,
        )
        fallback_end = (text_base or record["value"]) + (text_size or 0)
        record["end"] = next_value or fallback_end
    # Duplicate aliases at exactly the same address are useful evidence but
    # retaining every spelling creates unstable lookup order.  Keep unique
    # (address, name, end) tuples.
    unique: dict[tuple[int, str | None, int], dict[str, Any]] = {}
    for record in records:
        key = (record["value"], record["name"], record["end"])
        unique.setdefault(key, record)
    return list(unique.values())


def _stable_anchor_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{digest}"


def _source_from_die(
    die: _DwarfDie,
    *,
    cu: _DwarfDie | None,
    path_remaps: Sequence[tuple[str, str]],
) -> SourceLocation | None:
    raw_path = die.attrs.get("decl_file")
    if not isinstance(raw_path, str):
        raw_path = cu.attrs.get("name") if cu else None
    comp_dir = cu.attrs.get("comp_dir") if cu else None
    path = canonical_source_path(raw_path, comp_dir=comp_dir, path_remaps=path_remaps)
    line = die.attrs.get("decl_line")
    column = die.attrs.get("decl_column")
    discriminator = die.attrs.get("discriminator")
    if path is None and line is None and column is None:
        return None
    return SourceLocation(
        path,
        line if isinstance(line, int) else None,
        column if isinstance(column, int) else None,
        discriminator if isinstance(discriminator, int) else None,
    )


def _resolve_die_attr(dies: Mapping[int, _DwarfDie], die: _DwarfDie, key: str) -> Any:
    value = die.attrs.get(key)
    seen: set[int] = set()
    while isinstance(value, int) and value in dies and value not in seen:
        seen.add(value)
        origin = dies[value]
        candidate = origin.attrs.get(key)
        if candidate is not None:
            value = candidate
            break
        value = origin.attrs.get("abstract_origin")
    return value


def _parent_chain(dies: Mapping[int, _DwarfDie], die: _DwarfDie) -> list[_DwarfDie]:
    result: list[_DwarfDie] = []
    current = die.parent_offset
    while current is not None and current in dies:
        parent = dies[current]
        result.append(parent)
        current = parent.parent_offset
    return result


def _disassemble_backedges(
    elf: Path,
    *,
    text_base: int | None,
    text_size: int | None,
    functions: Sequence[Anchor],
    line_entries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover conservative backward branch targets when a disassembler exists."""

    objdump = _which(
        (
            os.environ.get("DWARF_SOURCE_OBJDUMP", ""),
            "llvm-objdump",
            "riscv64-unknown-linux-gnu-objdump",
        )
    )
    if not objdump or text_base is None or text_size is None:
        return []
    command = [objdump, "-d", "--triple=riscv64", str(elf)] if Path(objdump).name == "llvm-objdump" else [objdump, "-d", str(elf)]
    try:
        output, _ = _run_tool(command, timeout=180)
    except DwarfSourceError:
        return []

    function_ranges: list[tuple[int, int, Anchor]] = []
    for anchor in functions:
        if anchor.kind not in {"function", "symbol"}:
            continue
        for address_range in anchor.ranges:
            function_ranges.append((address_range.start, address_range.end, anchor))
    # Symbol/DWARF tables can contain aliases at one entry.  Deduplicate the
    # interval lookup before walking every disassembled branch; the naive
    # nested scan becomes quadratic for the large jemalloc images.
    unique_ranges: dict[tuple[int, int, str], tuple[int, int, Anchor]] = {}
    for start, end, anchor in function_ranges:
        unique_ranges.setdefault((start, end, anchor.anchor_id), (start, end, anchor))
    function_ranges = sorted(unique_ranges.values(), key=lambda item: (item[0], item[1], item[2].anchor_id))
    function_starts = [item[0] for item in function_ranges]
    line_sorted = sorted(line_entries, key=lambda item: int(item["pc"]))
    branch_re = re.compile(r"^\s*([0-9a-fA-F]+):\s+(?:[0-9a-fA-F]+(?:\s+|$))+([^\s]+)\s*(.*)$")
    backedges: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    branch_names = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez", "blez", "bgtz", "j"}

    def containing(pc: int) -> Anchor | None:
        index = bisect.bisect_right(function_starts, pc) - 1
        # Function ranges are normally disjoint.  Scan equal-start aliases and
        # a small overlap tail for unusual DWARF producers, keeping lookup
        # bounded even when a binary has many branch instructions.
        scanned = 0
        while index >= 0 and scanned < 32 and function_ranges[index][0] <= pc:
            start, end, anchor = function_ranges[index]
            if start <= pc < end:
                return anchor
            index -= 1
            scanned += 1
        return None

    def nearest_line(pc: int) -> SourceLocation | None:
        candidate = None
        for entry in line_sorted:
            if int(entry["pc"]) > pc:
                break
            candidate = SourceLocation.from_dict(entry.get("source"))
        return candidate

    for line in output.splitlines():
        match = branch_re.match(line)
        if not match:
            continue
        pc = int(match.group(1), 16)
        mnemonic = match.group(2).lower()
        operands = match.group(3)
        if mnemonic not in branch_names:
            continue
        target_match = re.search(r"(?:0x)?([0-9a-fA-F]+)(?:\s*<|$)", operands)
        if not target_match:
            continue
        target = int(target_match.group(1), 16)
        if target >= pc or target < text_base or target >= text_base + text_size:
            continue
        function = containing(pc)
        if function is None or not any(r.start <= target < r.end for r in function.ranges):
            continue
        key = (function.anchor_id, target)
        if key in seen:
            continue
        seen.add(key)
        source = nearest_line(target)
        identity = f"{function.anchor_id}|{source.path if source else ''}|{source.line if source else ''}|{target}"
        backedges.append(
            {
                "anchor_id": _stable_anchor_id("loop", identity),
                "function_anchor_id": function.anchor_id,
                "target": target,
                "branch_pc": pc,
                "source": source.to_dict() if source else None,
                "identity": identity,
            }
        )
    return backedges


def build_catalog(
    elf: str | os.PathLike[str],
    *,
    manifest: Mapping[str, Any] | str | os.PathLike[str] | None = None,
    path_remaps: Sequence[tuple[str, str]] = (),
    max_line_anchors: int = DEFAULT_MAX_LINE_ANCHORS,
    recover_loops: bool = True,
) -> AnchorCatalog:
    """Build a static source/function/loop catalog for ``elf``.

    The parser intentionally records both the artifact hash and tool commands,
    making a catalog auditable even when a producer has no GNU Build-ID note.
    """

    elf_path = Path(elf).resolve()
    if not elf_path.is_file():
        raise FileNotFoundError(elf_path)
    artifact_hash = sha256_file(elf_path)
    readelf = _which((os.environ.get("DWARF_SOURCE_READELF", ""), "readelf"))
    if not readelf:
        raise DwarfSourceError("readelf is required to inspect ELF metadata")
    llvm_dwarfdump = _which((os.environ.get("DWARF_SOURCE_DWARFDUMP", ""), "llvm-dwarfdump"))
    commands: list[list[str]] = []

    header_command = [readelf, "-h", "-SW", "-n", str(elf_path)]
    header_text, _ = _run_tool(header_command)
    commands.append(header_command)
    architecture, text_base, text_size, sections = _parse_elf_sections(header_text)
    build_id = _parse_build_id(header_text)
    build_identity = build_id or f"sha256:{artifact_hash}"

    symbol_command = [readelf, "-Ws", "--wide", str(elf_path)]
    symbol_text, _ = _run_tool(symbol_command)
    commands.append(symbol_command)
    symbols = _parse_symbols(
        symbol_text,
        text_base=text_base,
        text_size=text_size,
        path_remaps=path_remaps,
    )

    dies: list[_DwarfDie] = []
    dwarf_text = ""
    line_text = ""
    if llvm_dwarfdump and ".debug_info" in sections:
        info_command = [llvm_dwarfdump, "--debug-info", "--show-children", str(elf_path)]
        dwarf_text, _ = _run_tool(info_command)
        commands.append(info_command)
        if ".debug_line" in sections:
            line_command = [llvm_dwarfdump, "--debug-line", str(elf_path)]
            line_text, _ = _run_tool(line_command)
            commands.append(line_command)

    if not dwarf_text and ".debug_info" in sections:
        # A minimal readelf fallback keeps symbol-only operation available on
        # hosts without LLVM.  It has less rich string resolution but retains
        # function ranges and source line numbers where printed.
        info_command = [readelf, "--debug-dump=info", "--wide", str(elf_path)]
        dwarf_text, _ = _run_tool(info_command)
        commands.append(info_command)

    if dwarf_text:
        dies = parse_llvm_dwarf_info(dwarf_text)
    die_by_offset = {die.offset: die for die in dies}
    compile_units = [die for die in dies if die.tag == "compile_unit"]
    cu_for_die: dict[int, _DwarfDie | None] = {}
    for die in dies:
        current: _DwarfDie | None = die
        while current is not None and current.tag != "compile_unit":
            current = die_by_offset.get(current.parent_offset) if current.parent_offset is not None else None
        cu_for_die[die.offset] = current

    anchors: list[Anchor] = []
    anchor_by_die: dict[int, Anchor] = {}
    dwarf_function_ranges: list[tuple[int, int, Anchor]] = []
    for die in dies:
        if die.tag not in {"subprogram", "inlined_subroutine", "lexical_block"}:
            continue
        ranges = _die_ranges(die)
        if not ranges:
            continue
        cu = cu_for_die.get(die.offset)
        name = _resolve_die_attr(die_by_offset, die, "name")
        linkage_name = _resolve_die_attr(die_by_offset, die, "linkage_name")
        if not isinstance(name, str):
            name = None
        if not isinstance(linkage_name, str):
            linkage_name = None
        source = _source_from_die(die, cu=cu, path_remaps=path_remaps)
        parent_chain = _parent_chain(die_by_offset, die)
        parent_names = tuple(
            item_name
            for item in parent_chain
            if isinstance(item_name := _resolve_die_attr(die_by_offset, item, "name"), str)
        )
        if die.tag == "subprogram":
            kind = "function"
            identity = "|".join(
                (
                    kind,
                    source.path if source and source.path else "",
                    name or linkage_name or "<anonymous>",
                    linkage_name or "",
                    str(source.line if source else ""),
                    str(source.column if source else ""),
                )
            )
        elif die.tag == "inlined_subroutine":
            kind = "inline"
            call_path = _resolve_die_attr(die_by_offset, die, "call_file")
            call_line = _resolve_die_attr(die_by_offset, die, "call_line")
            call_source = SourceLocation(
                canonical_source_path(call_path, comp_dir=cu.attrs.get("comp_dir") if cu else None, path_remaps=path_remaps)
                if isinstance(call_path, str)
                else (source.path if source else None),
                call_line if isinstance(call_line, int) else (source.line if source else None),
                source.column if source else None,
                source.discriminator if source else None,
            )
            source = call_source
            identity = "|".join(
                (kind, source.path or "", name or linkage_name or (parent_names[0] if parent_names else "<inline>"), str(source.line or ""), str(source.column or ""))
            )
        else:
            explicit_loop = "loop" in die.tag.lower()
            # GCC commonly emits lexical_block for scopes rather than an
            # explicit loop DIE.  A lexical scope is not a reliable loop
            # marker, so omit it here; loop candidates are recovered later
            # from backward branches with an explicit low-confidence label.
            if not explicit_loop:
                continue
            kind = "loop"
            identity = "|".join(
                (kind, source.path if source and source.path else "", str(source.line if source else ""), str(source.column if source else ""), str(ranges[0].start))
            )
        anchor_id = _stable_anchor_id(kind, identity)
        confidence = "M" if source and kind in {"function", "inline", "loop"} else "L"
        anchor = Anchor(
            anchor_id=anchor_id,
            kind=kind,
            name=name,
            linkage_name=linkage_name,
            source=source,
            ranges=ranges,
            confidence=confidence,
            origin="dwarf",
            inline_chain=parent_names,
            image_relative_pc=(min(r.start for r in ranges) - text_base) if text_base is not None else None,
            die_offset=die.offset,
        )
        anchors.append(anchor)
        anchor_by_die[die.offset] = anchor
        if kind == "function":
            dwarf_function_ranges.extend((r.start, r.end, anchor) for r in ranges)

    # Add symbol-only function anchors for application code and stripped/low
    # debug regions.  A matching DWARF function wins at an overlapping address.
    for symbol in symbols:
        ranges = (AddressRange(symbol["value"], symbol["end"]),)
        overlaps_dwarf = any(
            symbol["value"] < end and start < symbol["end"] and (anchor.name == symbol["name"] or anchor.source is None)
            for start, end, anchor in dwarf_function_ranges
        )
        if overlaps_dwarf:
            continue
        source = SourceLocation(symbol["source_unit"], None, None, None) if symbol.get("source_unit") else None
        identity = "|".join(("symbol", source.path if source else "", symbol.get("name") or "", hex(symbol["value"])))
        anchors.append(
            Anchor(
                anchor_id=_stable_anchor_id("symbol", identity),
                kind="symbol",
                name=symbol.get("name"),
                linkage_name=symbol.get("name"),
                source=source,
                ranges=ranges,
                confidence="L",
                origin="symtab",
                image_relative_pc=(symbol["value"] - text_base) if text_base is not None else None,
            )
        )

    line_rows: dict[int, dict[str, Any]] = {}
    line_truncated = False
    if line_text:
        line_rows, line_truncated = _parse_llvm_line_table(
            line_text,
            path_remaps=path_remaps,
            max_entries=max_line_anchors,
        )
    line_entries = tuple(line_rows[pc] for pc in sorted(line_rows))

    # Add conservative static loop backedge anchors.  They are explicitly
    # marked as candidates unless a future runtime marker validates them.
    loop_backedges = _disassemble_backedges(
        elf_path,
        text_base=text_base,
        text_size=text_size,
        functions=anchors,
        line_entries=line_entries,
    ) if recover_loops else []
    known_ids = {anchor.anchor_id for anchor in anchors}
    for loop in loop_backedges:
        if loop["anchor_id"] in known_ids:
            continue
        source = SourceLocation.from_dict(loop.get("source"))
        anchors.append(
            Anchor(
                anchor_id=loop["anchor_id"],
                kind="loop_candidate",
                name=None,
                linkage_name=None,
                source=source,
                ranges=(AddressRange(loop["target"], loop["branch_pc"] + 1),),
                confidence="L",
                origin="static-disassembly",
                parent_anchor_id=loop.get("function_anchor_id"),
                inline_chain=(),
                image_relative_pc=(loop["target"] - text_base) if text_base is not None else None,
                event_kind="backedge_target",
                recovery="backward-branch",
            )
        )

    anchors.sort(key=lambda item: (min((r.start for r in item.ranges), default=0), item.kind, item.anchor_id))
    # Build a compact interval map.  A line entry is retained as source context
    # even when no DWARF DIE covers the address.
    pc_map_rows: list[PcMapping] = []
    for anchor in anchors:
        function_id = anchor.anchor_id if anchor.kind in {"function", "symbol"} else anchor.parent_anchor_id
        for address_range in anchor.ranges:
            pc_map_rows.append(PcMapping(address_range.start, address_range.end, (anchor.anchor_id,), function_id, anchor.source))
    for index, entry in enumerate(line_entries):
        start = int(entry["pc"])
        end = int(line_entries[index + 1]["pc"]) if index + 1 < len(line_entries) else start + 1
        if end <= start:
            end = start + 1
        pc_map_rows.append(PcMapping(start, end, (), None, SourceLocation.from_dict(entry.get("source"))))
    pc_map_rows.sort(key=lambda item: (item.start, item.end, item.anchor_ids))

    source_files = sorted(
        {
            anchor.source.path
            for anchor in anchors
            if anchor.source and anchor.source.path
        }
        | {
            entry["source"]["path"]
            for entry in line_entries
            if entry.get("source", {}).get("path")
        }
    )
    manifest_hash_value = manifest_sha256(manifest) if manifest is not None else None
    capabilities = {
        "debug_info": ".debug_info" in sections,
        "debug_line": ".debug_line" in sections,
        "symtab": ".symtab" in sections,
        "function_anchor_count": sum(anchor.kind == "function" for anchor in anchors),
        "symbol_anchor_count": sum(anchor.kind == "symbol" for anchor in anchors),
        "inline_anchor_count": sum(anchor.kind == "inline" for anchor in anchors),
        "loop_anchor_count": sum(anchor.kind == "loop" for anchor in anchors),
        "loop_candidate_count": sum(anchor.kind == "loop_candidate" for anchor in anchors),
        "source_line_count": len(line_entries),
        "source_file_count": len(source_files),
        "line_table_truncated": line_truncated,
        "dwarf_backend": "llvm-dwarfdump" if llvm_dwarfdump and dwarf_text else ("readelf" if dwarf_text else None),
        "loop_recovery": "static-backward-branch" if recover_loops else "disabled",
    }
    provenance = {
        "elf_sha256": artifact_hash,
        "manifest_hash": manifest_hash_value,
        "commands": commands,
        "tools": {
            "readelf": {"path": readelf, "version": _tool_version(readelf)},
            "llvm_dwarfdump": {"path": llvm_dwarfdump, "version": _tool_version(llvm_dwarfdump)} if llvm_dwarfdump else None,
        },
    }
    path_policy = {
        "version": 1,
        "explicit_remaps": [{"from": old, "to": new} for old, new in path_remaps],
        "absolute_fallback": "external/<sha256-prefix>/<basename>",
        "absolute_paths_emitted": False,
    }
    return AnchorCatalog(
        elf_path=str(elf_path),
        artifact_sha256=artifact_hash,
        build_id=build_id,
        build_identity=build_identity,
        manifest_hash=manifest_hash_value,
        architecture=architecture,
        text_base=text_base,
        text_size=text_size,
        anchors=tuple(anchors),
        pc_map=tuple(pc_map_rows),
        line_entries=line_entries,
        source_files=tuple(source_files),
        capabilities=capabilities,
        provenance=provenance,
        path_policy=path_policy,
    )


@dataclass(frozen=True)
class OccurrenceEvent:
    anchor_id: str
    occurrence: int
    event_phase: str = "before_instruction"
    pc: int | None = None
    workload_icount: int | None = None
    semantic_key: str | None = None
    context: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "occurrence": self.occurrence,
            "event_phase": self.event_phase,
            "pc": self.pc,
            "workload_icount": self.workload_icount,
            "semantic_key": self.semantic_key,
            "context": list(self.context),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OccurrenceEvent":
        return cls(
            anchor_id=str(value["anchor_id"]),
            occurrence=int(value.get("occurrence", 0)),
            event_phase=str(value.get("event_phase", "before_instruction")),
            pc=value.get("pc"),
            workload_icount=value.get("workload_icount"),
            semantic_key=value.get("semantic_key"),
            context=tuple(value.get("context", [])),
        )


@dataclass
class OccurrenceTrace:
    build_identity: str
    events: tuple[OccurrenceEvent, ...]
    event_phase: str = "before_instruction"
    source: str | None = None
    catalog_sha256: str | None = None
    unmatched_samples: int = 0
    reliability: str = "observed_pc_trace"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "build_identity": self.build_identity,
            "event_phase": self.event_phase,
            "source": self.source,
            "catalog_sha256": self.catalog_sha256,
            "unmatched_samples": self.unmatched_samples,
            "reliability": self.reliability,
            "metadata": self.metadata,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OccurrenceTrace":
        return cls(
            build_identity=str(value.get("build_identity", "")),
            events=tuple(OccurrenceEvent.from_dict(item) for item in value.get("events", [])),
            event_phase=str(value.get("event_phase", "before_instruction")),
            source=value.get("source"),
            catalog_sha256=value.get("catalog_sha256"),
            unmatched_samples=int(value.get("unmatched_samples", 0)),
            reliability=str(value.get("reliability", "unknown")),
            metadata=dict(value.get("metadata", {})),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "OccurrenceTrace":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_trace_samples(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        samples: list[Any] = []
        for line in text.splitlines():
            token = line.strip().split()[0] if line.strip() else ""
            if token:
                samples.append(int(token, 0))
        return samples
    if isinstance(value, Mapping):
        for key in ("samples", "pcs", "trace", "events"):
            if key in value and isinstance(value[key], list):
                return list(value[key])
        return []
    if isinstance(value, list):
        return value
    raise ValueError(f"trace must be a JSON list/object or whitespace list: {path}")


def collect_occurrence_trace(
    catalog: AnchorCatalog,
    samples: Iterable[int | Mapping[str, Any]],
    *,
    mode: str = "function-entry",
    event_phase: str = "before_instruction",
    source: str | None = None,
) -> OccurrenceTrace:
    """Collect sparse function/loop events from a PC trace.

    ``mode=function-entry`` emits an event only when a sample PC equals a
    function/symbol entry.  ``mode=anchor-hit`` emits the most specific anchor
    covering each sample.  The former is the default because it has a clear
    static event boundary; both modes retain an unmatched count.
    """

    if mode not in {"function-entry", "anchor-hit", "loop-entry"}:
        raise ValueError(f"unsupported collection mode: {mode}")
    entries = catalog.function_entries()
    by_loop_start: dict[int, Anchor] = {}
    for anchor in catalog.anchors:
        if anchor.kind in {"loop", "loop_candidate"} and anchor.ranges:
            by_loop_start.setdefault(min(r.start for r in anchor.ranges), anchor)
    counts: defaultdict[str, int] = defaultdict(int)
    events: list[OccurrenceEvent] = []
    unmatched = 0
    for sample in samples:
        if isinstance(sample, Mapping):
            raw_pc = sample.get("pc", sample.get("address"))
            pc = int(raw_pc, 0) if isinstance(raw_pc, str) else (int(raw_pc) if raw_pc is not None else None)
            icount = sample.get("workload_icount", sample.get("icount"))
            icount = int(icount) if icount is not None else None
            sample_phase = str(sample.get("event_phase", event_phase))
            explicit_anchor = sample.get("anchor_id")
        else:
            pc = int(sample)
            icount = None
            sample_phase = event_phase
            explicit_anchor = None
        anchor: Anchor | None = None
        if explicit_anchor:
            anchor = catalog.semantic_anchor(str(explicit_anchor))
        elif mode == "function-entry":
            anchor = entries.get(pc)
        elif mode == "loop-entry":
            anchor = by_loop_start.get(pc)
        elif pc is not None:
            candidates = catalog.lookup_pc(pc)
            anchor = candidates[0] if candidates else None
        if anchor is None:
            unmatched += 1
            continue
        occurrence = counts[anchor.anchor_id]
        counts[anchor.anchor_id] += 1
        semantic_key = _anchor_semantic_key(anchor)
        events.append(
            OccurrenceEvent(
                anchor_id=anchor.anchor_id,
                occurrence=occurrence,
                event_phase=sample_phase,
                pc=pc,
                workload_icount=icount,
                semantic_key=semantic_key,
            )
        )
    return OccurrenceTrace(
        build_identity=catalog.build_identity,
        events=tuple(events),
        event_phase=event_phase,
        source=source,
        catalog_sha256=canonical_json_hash(catalog.to_dict()),
        unmatched_samples=unmatched,
        reliability="observed_pc_trace" if events else "no_events",
        metadata={"mode": mode, "sample_count": len(events) + unmatched},
    )


class OccurrenceCollector(Protocol):
    def collect(self, samples: Iterable[int | Mapping[str, Any]]) -> OccurrenceTrace:
        ...


@dataclass
class PcTraceCollector:
    catalog: AnchorCatalog
    mode: str = "function-entry"
    event_phase: str = "before_instruction"

    def collect(self, samples: Iterable[int | Mapping[str, Any]]) -> OccurrenceTrace:
        return collect_occurrence_trace(
            self.catalog,
            samples,
            mode=self.mode,
            event_phase=self.event_phase,
        )


def _anchor_semantic_key(anchor: Anchor) -> str:
    source = anchor.source
    return "|".join(
        (
            anchor.kind,
            source.path if source and source.path else "",
            str(source.line if source and source.line is not None else ""),
            str(source.column if source and source.column is not None else ""),
            anchor.name or anchor.linkage_name or "",
        )
    )


def _as_event(value: OccurrenceEvent | Mapping[str, Any]) -> OccurrenceEvent:
    return value if isinstance(value, OccurrenceEvent) else OccurrenceEvent.from_dict(value)


@dataclass(frozen=True)
class AlignmentMatch:
    source_index: int
    target_index: int
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_index": self.source_index,
            "target_index": self.target_index,
            "score": self.score,
            "reason": self.reason,
        }


@dataclass
class SequenceAlignment:
    status: str
    reason: str | None
    score: float | None
    second_best_score: float | None
    global_path_margin: float | None
    matches: tuple[AlignmentMatch, ...]
    source_gaps: tuple[int, ...]
    target_gaps: tuple[int, ...]
    source_build: str | None = None
    target_build: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "reason": self.reason,
            "score": self.score,
            "second_best_score": self.second_best_score,
            "global_path_margin": self.global_path_margin,
            "matches": [match.to_dict() for match in self.matches],
            "source_gaps": list(self.source_gaps),
            "target_gaps": list(self.target_gaps),
            "source_build": self.source_build,
            "target_build": self.target_build,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class _Path:
    score: float
    ops: tuple[tuple[str, int, int, float, str], ...]


def _match_evidence(
    source: OccurrenceEvent,
    target: OccurrenceEvent,
    correspondence: Mapping[str, Sequence[str]] | None,
) -> tuple[float, str] | None:
    if source.event_phase != target.event_phase:
        return None
    if source.anchor_id == target.anchor_id:
        return 3.0, "anchor_id"
    if correspondence and target.anchor_id in set(correspondence.get(source.anchor_id, ())):
        return 2.5, "correspondence"
    if source.semantic_key and target.semantic_key and source.semantic_key == target.semantic_key:
        return 2.0, "semantic_key"
    # Context can be supplied by a native marker collector when IDs are
    # build-local.  Require a non-empty exact token to avoid broad fuzzy joins.
    if source.context and target.context and set(source.context) & set(target.context):
        return 0.5, "context"
    return None


def _path_sort_key(path: _Path) -> tuple[Any, ...]:
    # Match wins ties, then source/target gap order.  The operation indices make
    # the result independent of Python dict/set iteration order.
    rank = {"match": 0, "source_gap": 1, "target_gap": 2}
    return tuple((rank[op[0]], op[1], op[2]) for op in path.ops)


def align_occurrence_sequences(
    source_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    target_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    *,
    correspondence: Mapping[str, Sequence[str]] | None = None,
    gap_penalty: float = -1.0,
    max_cells: int = DEFAULT_MAX_ALIGNMENT_CELLS,
    source_build: str | None = None,
    target_build: str | None = None,
) -> SequenceAlignment:
    """Globally align two sparse event sequences with deterministic gaps.

    The DP is independent of any queried SimPoint set.  It retains two paths
    per cell so the reported margin is a global top-1/top-2 margin rather than
    a local candidate difference.  Events are only paired when their semantic
    evidence agrees; missing events become explicit gaps.
    """

    source = tuple(_as_event(item) for item in source_events)
    target = tuple(_as_event(item) for item in target_events)
    canonical_source = tuple(
        (event.semantic_key, event.occurrence, event.event_phase) for event in source
    )
    canonical_target = tuple(
        (event.semantic_key, event.occurrence, event.event_phase) for event in target
    )
    if (
        source
        and len(source) == len(target)
        and all(event.semantic_key for event in source)
        and canonical_source == canonical_target
    ):
        matches = tuple(
            AlignmentMatch(index, index, 2.0, "canonical_occurrence_identity")
            for index in range(len(source))
        )
        return SequenceAlignment(
            status="matched",
            reason=None,
            score=2.0 * len(matches),
            second_best_score=None,
            global_path_margin=None,
            matches=matches,
            source_gaps=(),
            target_gaps=(),
            source_build=source_build,
            target_build=target_build,
            diagnostics={
                "source_events": len(source),
                "target_events": len(target),
                "method": "canonical_occurrence_identity",
            },
        )
    cells = (len(source) + 1) * (len(target) + 1)
    if cells > max_cells:
        return SequenceAlignment(
            status="rejected",
            reason="SEARCH_TRUNCATED",
            score=None,
            second_best_score=None,
            global_path_margin=None,
            matches=(),
            source_gaps=(),
            target_gaps=(),
            source_build=source_build,
            target_build=target_build,
            diagnostics={"cells": cells, "max_cells": max_cells},
        )

    table: list[list[tuple[_Path, ...]]] = [[() for _ in range(len(target) + 1)] for _ in range(len(source) + 1)]
    table[0][0] = (_Path(0.0, ()),)
    for i in range(1, len(source) + 1):
        table[i][0] = (_Path((i * gap_penalty), tuple(("source_gap", k, -1, gap_penalty, "gap") for k in range(i))),)
    for j in range(1, len(target) + 1):
        table[0][j] = (_Path((j * gap_penalty), tuple(("target_gap", -1, k, gap_penalty, "gap") for k in range(j))),)

    for i in range(1, len(source) + 1):
        for j in range(1, len(target) + 1):
            candidates: list[_Path] = []
            for previous in table[i - 1][j]:
                candidates.append(_Path(previous.score + gap_penalty, previous.ops + (("source_gap", i - 1, -1, gap_penalty, "gap"),)))
            for previous in table[i][j - 1]:
                candidates.append(_Path(previous.score + gap_penalty, previous.ops + (("target_gap", -1, j - 1, gap_penalty, "gap"),)))
            evidence = _match_evidence(source[i - 1], target[j - 1], correspondence)
            if evidence is not None:
                match_score, reason = evidence
                for previous in table[i - 1][j - 1]:
                    candidates.append(_Path(previous.score + match_score, previous.ops + (("match", i - 1, j - 1, match_score, reason),)))
            # Deduplicate equal operation paths before retaining top two.
            unique: dict[tuple[tuple[str, int, int, float, str], ...], _Path] = {}
            for candidate in candidates:
                old = unique.get(candidate.ops)
                if old is None or candidate.score > old.score:
                    unique[candidate.ops] = candidate
            ranked = sorted(unique.values(), key=lambda item: (-item.score, _path_sort_key(item)))
            table[i][j] = tuple(ranked[:2])

    final_paths = table[-1][-1]
    if not final_paths:
        return SequenceAlignment(
            status="rejected",
            reason="NO_CANDIDATE",
            score=None,
            second_best_score=None,
            global_path_margin=None,
            matches=(),
            source_gaps=tuple(range(len(source))),
            target_gaps=tuple(range(len(target))),
            source_build=source_build,
            target_build=target_build,
        )
    best = final_paths[0]
    second = final_paths[1].score if len(final_paths) > 1 else None
    matches = tuple(
        AlignmentMatch(op[1], op[2], op[3], op[4])
        for op in best.ops
        if op[0] == "match"
    )
    source_gaps = tuple(op[1] for op in best.ops if op[0] == "source_gap")
    target_gaps = tuple(op[2] for op in best.ops if op[0] == "target_gap")
    status = "matched" if matches else "rejected"
    reason = None if matches else "NO_CANDIDATE"
    # Equal-scoring complete paths mean that a repeated phase cannot be
    # uniquely identified.  Keep the evidence for audit, but reject the
    # position instead of silently choosing the deterministic tie-break path.
    if second is not None and best.score - second <= 1e-12:
        status = "rejected"
        reason = "AMBIGUOUS"
    # Validate strict sequence order explicitly, even though DP construction
    # normally guarantees it; this protects callers passing unusual subclasses.
    if any(a.source_index >= b.source_index or a.target_index >= b.target_index for a, b in zip(matches, matches[1:])):
        status = "rejected"
        reason = "CROSSING"
    return SequenceAlignment(
        status=status,
        reason=reason,
        score=best.score,
        second_best_score=second,
        global_path_margin=(best.score - second) if second is not None else None,
        matches=matches,
        source_gaps=source_gaps,
        target_gaps=target_gaps,
        source_build=source_build,
        target_build=target_build,
        diagnostics={"source_events": len(source), "target_events": len(target), "cells": cells, "gap_penalty": gap_penalty},
    )


def align_occurrence_sequences_segmented(
    source_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    target_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    *,
    sync_semantic_keys: Sequence[str] | None = None,
    gap_penalty: float = -1.0,
    max_cells: int = DEFAULT_MAX_ALIGNMENT_CELLS,
    source_build: str | None = None,
    target_build: str | None = None,
) -> SequenceAlignment:
    """Align large traces by splitting at globally unique semantic sync events.

    A sync event is accepted only when its semantic key occurs exactly once in
    each trace and the ordered sync keys are identical.  Every interval between
    sync events is still aligned by the regular top-two DP, so ambiguity or a
    budget overflow in any segment rejects the complete result.  This avoids
    silently treating a repeated anchor's ordinal as cross-build identity.
    """

    source = tuple(_as_event(item) for item in source_events)
    target = tuple(_as_event(item) for item in target_events)
    direct = align_occurrence_sequences(
        source,
        target,
        gap_penalty=gap_penalty,
        max_cells=max_cells,
        source_build=source_build,
        target_build=target_build,
    )
    if not sync_semantic_keys and (
        direct.status == "matched" or direct.reason != "SEARCH_TRUNCATED"
    ):
        return direct
    requested = set(sync_semantic_keys or ())
    source_sync = [
        (index, event)
        for index, event in enumerate(source)
        if event.semantic_key and event.semantic_key in requested
    ]
    target_sync = [
        (index, event)
        for index, event in enumerate(target)
        if event.semantic_key and event.semantic_key in requested
    ]
    source_counts = defaultdict(int)
    target_counts = defaultdict(int)
    for _, event in source_sync:
        source_counts[event.semantic_key] += 1
    for _, event in target_sync:
        target_counts[event.semantic_key] += 1
    unique_keys = [
        key for key in requested
        if source_counts[key] == 1 and target_counts[key] == 1
    ]
    source_pairs = [item for item in source_sync if item[1].semantic_key in unique_keys]
    target_pairs = [item for item in target_sync if item[1].semantic_key in unique_keys]
    source_keys = [event.semantic_key for _, event in source_pairs]
    target_keys = [event.semantic_key for _, event in target_pairs]
    if not source_pairs or source_keys != target_keys:
        return SequenceAlignment(
            status="rejected",
            reason="NO_SYNC_ANCHOR" if not source_pairs else "AMBIGUOUS",
            score=None,
            second_best_score=None,
            global_path_margin=None,
            matches=(),
            source_gaps=tuple(range(len(source))),
            target_gaps=tuple(range(len(target))),
            source_build=source_build,
            target_build=target_build,
            diagnostics={
                "source_events": len(source),
                "target_events": len(target),
                "requested_sync_keys": sorted(requested),
                "unique_sync_keys": sorted(unique_keys),
            },
        )

    boundaries = [(-1, -1)] + [(source_pairs[i][0], target_pairs[i][0]) for i in range(len(source_pairs))]
    all_matches: list[AlignmentMatch] = []
    all_source_gaps: list[int] = []
    all_target_gaps: list[int] = []
    total_score = 0.0
    margins: list[float] = []
    segment_diagnostics: list[dict[str, Any]] = []
    for segment_index, ((previous_source, previous_target), (sync_source, sync_target)) in enumerate(
        zip(boundaries, boundaries[1:])
    ):
        segment_source_start = previous_source + 1
        segment_target_start = previous_target + 1
        segment_source_end = sync_source
        segment_target_end = sync_target
        segment = align_occurrence_sequences(
            source[segment_source_start:segment_source_end],
            target[segment_target_start:segment_target_end],
            gap_penalty=gap_penalty,
            max_cells=max_cells,
            source_build=source_build,
            target_build=target_build,
        )
        segment_diagnostics.append(segment.to_dict())
        if segment.status == "rejected" and segment.reason not in {"NO_CANDIDATE"}:
            return SequenceAlignment(
                status="rejected",
                reason=segment.reason,
                score=None,
                second_best_score=None,
                global_path_margin=None,
                matches=(),
                source_gaps=(),
                target_gaps=(),
                source_build=source_build,
                target_build=target_build,
                diagnostics={"segments": segment_diagnostics},
            )
        if segment.status == "rejected" and segment.reason == "SEARCH_TRUNCATED":
            return SequenceAlignment(
                status="rejected",
                reason="SEARCH_TRUNCATED",
                score=None,
                second_best_score=None,
                global_path_margin=None,
                matches=(),
                source_gaps=(),
                target_gaps=(),
                source_build=source_build,
                target_build=target_build,
                diagnostics={"segments": segment_diagnostics},
            )
        if segment.status == "matched":
            all_matches.extend(
                AlignmentMatch(match.source_index + segment_source_start, match.target_index + segment_target_start, match.score, match.reason)
                for match in segment.matches
            )
        all_source_gaps.extend(index + segment_source_start for index in segment.source_gaps)
        all_target_gaps.extend(index + segment_target_start for index in segment.target_gaps)
        if segment.score is not None:
            total_score += segment.score
        if segment.global_path_margin is not None:
            margins.append(segment.global_path_margin)
        sync_event_source = source[sync_source]
        sync_event_target = target[sync_target]
        if _match_evidence(sync_event_source, sync_event_target, None) is None:
            return SequenceAlignment(
                status="rejected",
                reason="INCOMPATIBLE_RUN",
                score=None,
                second_best_score=None,
                global_path_margin=None,
                matches=(),
                source_gaps=(),
                target_gaps=(),
                source_build=source_build,
                target_build=target_build,
                diagnostics={"segments": segment_diagnostics},
            )
        all_matches.append(AlignmentMatch(sync_source, sync_target, 2.0, "unique_sync"))
        total_score += 2.0

    # The final tail after the last synchronization event is another segment.
    tail = align_occurrence_sequences(
        source[source_pairs[-1][0] + 1:],
        target[target_pairs[-1][0] + 1:],
        gap_penalty=gap_penalty,
        max_cells=max_cells,
        source_build=source_build,
        target_build=target_build,
    )
    segment_diagnostics.append(tail.to_dict())
    if tail.reason == "SEARCH_TRUNCATED":
        return SequenceAlignment(
            status="rejected", reason="SEARCH_TRUNCATED", score=None,
            second_best_score=None, global_path_margin=None, matches=(),
            source_gaps=(), target_gaps=(), source_build=source_build,
            target_build=target_build, diagnostics={"segments": segment_diagnostics},
        )
    if tail.reason == "AMBIGUOUS":
        return SequenceAlignment(
            status="rejected", reason="AMBIGUOUS", score=None,
            second_best_score=None, global_path_margin=None, matches=(),
            source_gaps=(), target_gaps=(), source_build=source_build,
            target_build=target_build, diagnostics={"segments": segment_diagnostics},
        )
    if tail.status == "matched":
        offset_source = source_pairs[-1][0] + 1
        offset_target = target_pairs[-1][0] + 1
        all_matches.extend(
            AlignmentMatch(match.source_index + offset_source, match.target_index + offset_target, match.score, match.reason)
            for match in tail.matches
        )
    all_source_gaps.extend(index + source_pairs[-1][0] + 1 for index in tail.source_gaps)
    all_target_gaps.extend(index + target_pairs[-1][0] + 1 for index in tail.target_gaps)
    if tail.score is not None:
        total_score += tail.score
    if tail.global_path_margin is not None:
        margins.append(tail.global_path_margin)
    return SequenceAlignment(
        status="matched",
        reason=None,
        score=total_score,
        second_best_score=None,
        global_path_margin=min(margins) if margins else None,
        matches=tuple(all_matches),
        source_gaps=tuple(all_source_gaps),
        target_gaps=tuple(all_target_gaps),
        source_build=source_build,
        target_build=target_build,
        diagnostics={
            "source_events": len(source),
            "target_events": len(target),
            "segments": segment_diagnostics,
            "unique_sync_keys": sorted(unique_keys),
        },
    )


def project_event(
    source_index: int,
    source_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    target_events: Sequence[OccurrenceEvent | Mapping[str, Any]],
    alignment: SequenceAlignment,
) -> dict[str, Any]:
    """Project one source event, preserving exact/snapped/interpolated state."""

    source = tuple(_as_event(item) for item in source_events)
    target = tuple(_as_event(item) for item in target_events)
    if source_index < 0 or source_index >= len(source):
        return {"status": "rejected", "reason": "OUT_OF_TRACE", "source_index": source_index}
    exact = next((match for match in alignment.matches if match.source_index == source_index), None)
    if exact is not None:
        event = target[exact.target_index]
        return {
            "status": "matched",
            "position_fidelity": "exact",
            "source_index": source_index,
            "target_index": exact.target_index,
            "source_event": source[source_index].to_dict(),
            "target_event": event.to_dict(),
            "snap_delta_events": 0,
        }
    before = [match for match in alignment.matches if match.source_index < source_index]
    after = [match for match in alignment.matches if match.source_index > source_index]
    left = before[-1] if before else None
    right = after[0] if after else None
    if left is None and right is None:
        return {"status": "rejected", "reason": "NO_ANCHOR", "source_index": source_index}
    if left is not None and right is not None and source_index - left.source_index == right.source_index - source_index:
        return {
            "status": "rejected",
            "position_fidelity": "interpolated",
            "reason": "AMBIGUOUS",
            "source_index": source_index,
            "bracket": {"left_target_index": left.target_index, "right_target_index": right.target_index},
        }
    nearest = left if right is None or (left is not None and source_index - left.source_index < right.source_index - source_index) else right
    assert nearest is not None
    event = target[nearest.target_index]
    return {
        "status": "matched",
        "position_fidelity": "snapped",
        "source_index": source_index,
        "target_index": nearest.target_index,
        "source_event": source[source_index].to_dict(),
        "target_event": event.to_dict(),
        "snap_delta_events": abs(source_index - nearest.source_index),
    }


def _fixture() -> dict[str, Any]:
    """Return a tiny controlled positive with a deliberate target insertion."""

    source = [
        OccurrenceEvent("function:load", 0, semantic_key="function|src/work.c|10||load"),
        OccurrenceEvent("loop:step", 0, semantic_key="loop|src/work.c|20||step"),
        OccurrenceEvent("loop:step", 1, semantic_key="loop|src/work.c|20||step"),
        OccurrenceEvent("function:store", 0, semantic_key="function|src/work.c|40||store"),
    ]
    target = [
        OccurrenceEvent("function:load", 0, semantic_key="function|src/work.c|10||load"),
        OccurrenceEvent("loop:step", 0, semantic_key="loop|src/work.c|20||step"),
        OccurrenceEvent("loop:extra", 0, semantic_key="loop|src/work.c|25||extra"),
        OccurrenceEvent("loop:step", 1, semantic_key="loop|src/work.c|20||step"),
        OccurrenceEvent("function:store", 0, semantic_key="function|src/work.c|40||store"),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "controlled_positive",
        "source_build": "fixture-A",
        "target_build": "fixture-B",
        "event_phase": "before_instruction",
        "source_events": [event.to_dict() for event in source],
        "target_events": [event.to_dict() for event in target],
        "gold_matches": [
            {"source_index": 0, "target_index": 0, "expected": "exact"},
            {"source_index": 1, "target_index": 1, "expected": "exact"},
            {"source_index": 2, "target_index": 3, "expected": "exact"},
            {"source_index": 3, "target_index": 4, "expected": "exact"},
        ],
        "notes": "Target contains one explicit loop event insertion; occurrence values remain build-local.",
    }


def _catalog_suite(args: argparse.Namespace) -> dict[str, Any]:
    suite = Path(args.suite)
    suite_manifest = suite / "suite-manifest.json"
    manifest_input: Path | None = suite_manifest if suite_manifest.is_file() else None
    workloads = args.workloads or ["lbm", "mcf", "astar_biglakes", "libquantum", "bwaves", "cactusADM"]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "collector": {"name": COLLECTOR_NAME, "version": COLLECTOR_VERSION},
        "suite": str(suite.resolve()),
        "workloads": {},
        "limitations": [
            "The profile ELFs are inspected as supplied; no runtime marker trigger is installed.",
            "Symbol-only workload functions are retained at L confidence until a dynamic semantic marker validates them.",
        ],
        "manifest": {
            "path": str(manifest_input.resolve()) if manifest_input else None,
            "sha256": manifest_sha256(manifest_input) if manifest_input else None,
        },
    }
    catalog_dir = Path(args.catalog_dir) if args.catalog_dir else None
    for workload in workloads:
        sides: dict[str, Any] = {}
        for side in ("A", "B"):
            elf = suite / "workloads" / workload / side / "elf" / f"{workload}.elf"
            if not elf.is_file():
                sides[side] = {"status": "missing", "path": str(elf)}
                continue
            catalog = build_catalog(
                elf,
                manifest=manifest_input,
                path_remaps=args.path_remap,
                recover_loops=not args.no_loops,
            )
            if catalog_dir:
                output = catalog_dir / workload / f"{side}.catalog.json"
                catalog.save(output)
                catalog_path = str(output)
            else:
                catalog_path = None
            sides[side] = {
                "status": "ok",
                "elf": str(elf),
                "build_identity": catalog.build_identity,
                "sha256": catalog.artifact_sha256,
                "manifest_hash": catalog.manifest_hash,
                "architecture": catalog.architecture,
                "anchor_count": len(catalog.anchors),
                "source_file_count": len(catalog.source_files),
                "capabilities": catalog.capabilities,
                "catalog_path": catalog_path,
            }
        summary["workloads"][workload] = sides
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    catalog = sub.add_parser("catalog", help="build one ELF anchor catalog")
    catalog.add_argument("--elf", required=True, type=Path)
    catalog.add_argument("--output", required=True, type=Path)
    catalog.add_argument("--manifest", type=Path)
    catalog.add_argument("--path-remap", action="append", default=[], type=parse_path_remap)
    catalog.add_argument("--max-line-anchors", type=int, default=DEFAULT_MAX_LINE_ANCHORS)
    catalog.add_argument("--no-loops", action="store_true")

    suite = sub.add_parser("catalog-suite", help="catalog the six-workload experiment suite")
    suite.add_argument("--suite", type=Path, default=Path("experiment/multi-workload"))
    suite.add_argument("--output", required=True, type=Path)
    suite.add_argument("--catalog-dir", type=Path)
    suite.add_argument("--workloads", nargs="*")
    suite.add_argument("--path-remap", action="append", default=[], type=parse_path_remap)
    suite.add_argument("--no-loops", action="store_true")

    collect = sub.add_parser("collect", help="collect occurrence events from a PC trace")
    collect.add_argument("--catalog", required=True, type=Path)
    collect.add_argument("--trace", required=True, type=Path)
    collect.add_argument("--output", required=True, type=Path)
    collect.add_argument("--mode", choices=("function-entry", "anchor-hit", "loop-entry"), default="function-entry")
    collect.add_argument("--event-phase", default="before_instruction")

    align = sub.add_parser("align", help="globally align two occurrence traces")
    align.add_argument("--source-trace", required=True, type=Path)
    align.add_argument("--target-trace", required=True, type=Path)
    align.add_argument("--output", required=True, type=Path)
    align.add_argument("--correspondence", type=Path)
    align.add_argument("--gap-penalty", type=float, default=-1.0)
    align.add_argument("--max-cells", type=int, default=DEFAULT_MAX_ALIGNMENT_CELLS)

    fixture = sub.add_parser("fixture", help="write the controlled positive fixture")
    fixture.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "catalog":
        manifest = args.manifest if args.manifest else None
        catalog = build_catalog(
            args.elf,
            manifest=manifest,
            path_remaps=args.path_remap,
            max_line_anchors=args.max_line_anchors,
            recover_loops=not args.no_loops,
        )
        catalog.save(args.output)
        print(json.dumps({"output": str(args.output), "build_identity": catalog.build_identity, "capabilities": catalog.capabilities}, indent=2, sort_keys=True))
        return 0
    if args.command == "catalog-suite":
        summary = _catalog_suite(args)
        _write_json(args.output, summary)
        print(json.dumps({"output": str(args.output), "workloads": sorted(summary["workloads"])}, indent=2, sort_keys=True))
        return 0
    if args.command == "collect":
        catalog = AnchorCatalog.load(args.catalog)
        samples = _load_trace_samples(args.trace)
        trace = collect_occurrence_trace(catalog, samples, mode=args.mode, event_phase=args.event_phase, source=str(args.trace))
        trace.save(args.output)
        print(json.dumps({"output": str(args.output), "events": len(trace.events), "unmatched_samples": trace.unmatched_samples}, indent=2, sort_keys=True))
        return 0
    if args.command == "align":
        source = OccurrenceTrace.load(args.source_trace)
        target = OccurrenceTrace.load(args.target_trace)
        correspondence = None
        if args.correspondence:
            correspondence = json.loads(args.correspondence.read_text(encoding="utf-8"))
        result = align_occurrence_sequences(
            source.events,
            target.events,
            correspondence=correspondence,
            gap_penalty=args.gap_penalty,
            max_cells=args.max_cells,
            source_build=source.build_identity,
            target_build=target.build_identity,
        )
        _write_json(args.output, result.to_dict())
        print(json.dumps({"output": str(args.output), "status": result.status, "matches": len(result.matches), "margin": result.global_path_margin}, indent=2, sort_keys=True))
        return 0
    if args.command == "fixture":
        _write_json(args.output, _fixture())
        print(json.dumps({"output": str(args.output), "kind": "controlled_positive"}, indent=2, sort_keys=True))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DwarfSourceError, ValueError, FileNotFoundError) as exc:
        print(f"dwarf_source: {exc}", file=sys.stderr)
        raise SystemExit(2)
