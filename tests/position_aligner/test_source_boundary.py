import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checkpoint_align.dwarf_source import AddressRange, Anchor, AnchorCatalog, SourceLocation
from checkpoint_align.position_aligner.materialize import run_nemu_targets, write_nemu_batch_config
from checkpoint_align.position_aligner.source_boundary import (
    BoundaryPolicy,
    CheckpointPoint,
    NemuSourceProbeRunner,
    ProbeRun,
    SourceBoundaryResolver,
    _MarkerIndex,
)


def catalog(build: str, pc: int, *, line: int = 7, extra_rows=()) -> AnchorCatalog:
    anchor = Anchor(f"{build}:fn", "function", "fn", None, SourceLocation("src/a.c", line, 2, 0), (AddressRange(pc, pc + 64),), "M", "fixture")
    rows = ({"pc": pc, "source": {"path": "src/a.c", "line": line, "column": 2, "discriminator": 0}}, *extra_rows)
    return AnchorCatalog(build, "a" * 64, build, build, None, "RISC-V", 0, 1, (anchor,), (), rows, (), {}, {}, {})


def multi_address_catalog(build: str, artifact_sha256: str, *, second_pc: int) -> AnchorCatalog:
    """Two marker rows for one source line: the same key at two addresses."""
    anchors = (
        Anchor(f"{build}:fn", "function", "fn", None, SourceLocation("src/a.c", 8, 4, 0), (AddressRange(0x1000, 0x1100),), "M", "fixture"),
    )
    rows = (
        {"pc": 0x1000, "source": {"path": "src/a.c", "line": 8, "column": 4, "discriminator": 0}},
        {"pc": second_pc, "source": {"path": "src/a.c", "line": 8, "column": 4, "discriminator": 0}},
    )
    return AnchorCatalog(build, artifact_sha256, build, build, None, "RISC-V", 0x1000, 0x100, anchors, (), rows, (), {}, {}, {})


def run(document, name: str) -> ProbeRun:
    return ProbeRun(document, f"/{name}.json", name * 64)


def marker_catalog(build: str, pc: int, anchors, *, line: int = 7, column: int = 2) -> AnchorCatalog:
    rows = ({"pc": pc, "source": {"path": "src/a.c", "line": line, "column": column, "discriminator": 0}},)
    return AnchorCatalog(build, "a" * 64, build, build, None, "RISC-V", pc, 0x100, tuple(anchors), (), rows, (), {}, {}, {})


def boundary(*probes) -> ProbeRun:
    return run({"schema_version": 2, "mode": "probe-boundary-pcs", "event_phase": "before_instruction", "complete": True, "probes": list(probes)}, "b")


def occurrence(*probes, complete=True) -> ProbeRun:
    return run({"schema_version": 2, "mode": "probe-boundary-occurrences", "event_phase": "before_instruction", "complete": complete, "probes": list(probes)}, "o")


class FakeRunner:
    def __init__(self, boundary_run: ProbeRun, occurrence_run: ProbeRun | None = None):
        self.boundary_run = boundary_run
        self.occurrence_run = occurrence_run
        self.calls = []

    def probe_boundaries(self, points):
        self.calls.append(("boundary", points))
        return self.boundary_run

    def probe_occurrences(self, points):
        self.calls.append(("occurrence", points))
        if self.occurrence_run is None:
            raise AssertionError("unexpected occurrence pass")
        return self.occurrence_run


SOURCE_RUN = {"event_phase": "before_instruction", "interval_instructions": 20}
POLICY = BoundaryPolicy(interval_instructions=20)


class SourceBoundaryTest(unittest.TestCase):
    def resolve(self, runner, points=(CheckpointPoint(1, 100),), source=None, target=None, policy=POLICY):
        return SourceBoundaryResolver(runner).resolve(points, SOURCE_RUN, source or catalog("A", 0x1000), target or catalog("B", 0x2000), policy)

    def test_resolves_through_both_runner_passes(self):
        runner = FakeRunner(
            boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}),
            occurrence({"id": 1, "requested_icount": 100, "boundary_pc": 0x1000, "marker_pc": 0x1000, "marker_hit_icount": 95, "occurrence": 3, "complete": True}),
        )
        result = self.resolve(runner)
        item = result.items[0]
        self.assertEqual([call[0] for call in runner.calls], ["boundary", "occurrence"])
        self.assertEqual(item.status, "resolved")
        self.assertEqual(item.target_marker["pc"], 0x2000)
        self.assertEqual(item.marker_delta_instructions, -5)
        self.assertEqual(item.occurrence, 3)

    def test_old_last_watch_fallback_is_rejected_by_displacement(self):
        runner = FakeRunner(
            boundary({"id": 1, "requested_icount": 360, "observed_icount": 360, "pc": 0x1000, "complete": True}),
            occurrence({"id": 1, "requested_icount": 360, "boundary_pc": 0x1000, "marker_pc": 0x1000, "marker_hit_icount": 40, "occurrence": 0, "complete": True}),
        )
        result = self.resolve(runner, (CheckpointPoint(1, 360),))
        self.assertEqual(result.items[0].reason, "SOURCE_DISPLACEMENT_EXCEEDED")

    def test_rejects_nondeterministic_boundary(self):
        runner = FakeRunner(
            boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}),
            occurrence({"id": 1, "requested_icount": 100, "boundary_pc": 0x1004, "marker_pc": 0x1000, "marker_hit_icount": 95, "occurrence": 3, "complete": True}),
        )
        self.assertEqual(self.resolve(runner).items[0].reason, "NONDETERMINISTIC_SOURCE_RUN")

    def test_rejects_marker_not_entered(self):
        runner = FakeRunner(
            boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}),
            occurrence({"id": 1, "requested_icount": 100, "boundary_pc": 0x1000, "requested_watch_id": 0, "complete": False}, complete=False),
        )
        self.assertEqual(self.resolve(runner).items[0].reason, "MARKER_NOT_ENTERED")

    def test_rejects_missing_portable_marker_without_second_pass(self):
        runner = FakeRunner(boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}))
        result = self.resolve(runner, target=catalog("B", 0x2000, line=9))
        self.assertEqual(result.items[0].reason, "NO_PORTABLE_MARKER")
        self.assertEqual([call[0] for call in runner.calls], ["boundary"])

    def test_rejects_ambiguous_target_marker(self):
        extra = ({"pc": 0x2010, "source": {"path": "src/a.c", "line": 7, "column": 2, "discriminator": 0}},)
        runner = FakeRunner(boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}))
        self.assertEqual(self.resolve(runner, target=catalog("B", 0x2000, extra_rows=extra)).items[0].reason, "AMBIGUOUS_TARGET_MARKER")

    def test_rejects_ambiguous_source_marker(self):
        extra = ({"pc": 0x1010, "source": {"path": "src/a.c", "line": 7, "column": 2, "discriminator": 0}},)
        runner = FakeRunner(boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True}))
        self.assertEqual(self.resolve(runner, source=catalog("A", 0x1000, extra_rows=extra)).items[0].reason, "AMBIGUOUS_SOURCE_MARKER")

    def test_identical_range_symbol_alias_keeps_the_dwarf_context(self):
        unnamed_function = Anchor(
            "A:function", "function", None, None, SourceLocation("<artificial>", None, None, None),
            (AddressRange(0x1000, 0x1040),), "M", "dwarf", inline_chain=("<artificial>",),
        )
        symbol_alias = Anchor(
            "A:symbol", "symbol", "sort_basket", "sort_basket", None,
            (AddressRange(0x1000, 0x1040),), "L", "symtab",
        )
        index = _MarkerIndex(marker_catalog("A", 0x1000, (unnamed_function, symbol_alias), line=84, column=20))
        marker, key, reason = index.marker(0x1000)
        self.assertIsNone(reason)
        self.assertEqual(key, "src/a.c|84|20|0||<artificial>")
        self.assertEqual(marker["pc"], 0x1000)
        self.assertEqual(marker["inline_chain"], ["<artificial>"])

    def test_self_inline_instance_over_the_same_range_keeps_the_inline_entry(self):
        function = Anchor(
            "A:function", "function", "sort_basket", "sort_basket", SourceLocation("src/a.c", None, None, None),
            (AddressRange(0x1000, 0x1040),), "M", "dwarf", inline_chain=("src/a.c",),
        )
        inline = Anchor(
            "A:inline", "inline", None, None, SourceLocation("src/a.c", None, None, None),
            (AddressRange(0x1000, 0x1040),), "M", "dwarf", inline_chain=("sort_basket", "src/a.c"),
        )
        index = _MarkerIndex(marker_catalog("A", 0x1000, (function, inline)))
        marker, key, reason = index.marker(0x1000)
        self.assertIsNone(reason)
        self.assertEqual(key, "src/a.c|7|2|0||sort_basket|src/a.c")
        self.assertEqual(marker["inline_chain"], ["sort_basket", "src/a.c"])

    def test_distinct_equal_width_contexts_are_still_rejected(self):
        left = Anchor(
            "A:left", "inline", None, None, SourceLocation("src/a.c", None, None, None),
            (AddressRange(0xFF8, 0x1008),), "M", "dwarf", inline_chain=("alpha", "src/a.c"),
        )
        right = Anchor(
            "A:right", "inline", None, None, SourceLocation("src/a.c", None, None, None),
            (AddressRange(0x1000, 0x1010),), "M", "dwarf", inline_chain=("beta", "src/a.c"),
        )
        index = _MarkerIndex(marker_catalog("A", 0x1000, (left, right)))
        marker, key, reason = index.marker(0x1000)
        self.assertIsNone(marker)
        self.assertIsNone(key)
        self.assertEqual(reason, "UNSUPPORTED_INLINE_CONTEXT")

    def test_multi_address_marker_is_rejected_by_default(self):
        runner = FakeRunner(boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1010, "complete": True}))
        source = multi_address_catalog("A", "a" * 64, second_pc=0x1020)
        target = multi_address_catalog("B", "a" * 64, second_pc=0x1020)
        result = self.resolve(runner, source=source, target=target)
        self.assertEqual(result.items[0].reason, "AMBIGUOUS_SOURCE_MARKER")
        self.assertEqual([call[0] for call in runner.calls], ["boundary"])

    def test_multi_address_marker_pairs_by_address_for_identical_elfs(self):
        runner = FakeRunner(
            boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1010, "complete": True}),
            occurrence({"id": 1, "requested_icount": 100, "boundary_pc": 0x1010, "marker_pc": 0x1000, "marker_hit_icount": 100, "occurrence": 7, "complete": True}),
        )
        source = multi_address_catalog("A", "a" * 64, second_pc=0x1020)
        target = multi_address_catalog("B", "a" * 64, second_pc=0x1020)
        result = self.resolve(runner, source=source, target=target, policy=BoundaryPolicy(interval_instructions=20, multi_address_policy="identical-elf"))
        item = result.items[0]
        self.assertEqual(item.status, "resolved")
        self.assertEqual(item.multi_address_disambiguation, "identical_elf_address")
        self.assertEqual(item.source_marker["pc"], 0x1000)
        self.assertEqual(item.target_marker["pc"], 0x1000)
        self.assertEqual(item.occurrence, 7)

    def test_multi_address_policy_rejects_differing_elfs(self):
        runner = FakeRunner(boundary({"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1010, "complete": True}))
        source = multi_address_catalog("A", "a" * 64, second_pc=0x1020)
        target = multi_address_catalog("B", "b" * 64, second_pc=0x1020)
        with self.assertRaisesRegex(ValueError, "requires identical source and target ELF content"):
            self.resolve(runner, source=source, target=target, policy=BoundaryPolicy(interval_instructions=20, multi_address_policy="identical-elf"))
        self.assertEqual(runner.calls, [])

    def test_rejects_all_colliding_targets(self):
        runner = FakeRunner(
            boundary(
                {"id": 1, "requested_icount": 100, "observed_icount": 100, "pc": 0x1000, "complete": True},
                {"id": 2, "requested_icount": 110, "observed_icount": 110, "pc": 0x1000, "complete": True},
            ),
            occurrence(
                {"id": 1, "requested_icount": 100, "boundary_pc": 0x1000, "marker_pc": 0x1000, "marker_hit_icount": 95, "occurrence": 3, "complete": True},
                {"id": 2, "requested_icount": 110, "boundary_pc": 0x1000, "marker_pc": 0x1000, "marker_hit_icount": 105, "occurrence": 3, "complete": True},
            ),
        )
        result = self.resolve(runner, (CheckpointPoint(1, 100), CheckpointPoint(2, 110)))
        self.assertEqual([item.reason for item in result.items], ["TARGET_POSITION_COLLISION", "TARGET_POSITION_COLLISION"])

    def test_rejects_duplicate_batch_targets_before_nemu(self):
        with self.assertRaisesRegex(ValueError, "duplicate target position"):
            write_nemu_batch_config("/tmp/semantic-position-duplicate.txt", [{"checkpoint_id": 1, "target": {"anchor_id": "x", "pc": 1, "occurrence": 0}}, {"checkpoint_id": 2, "target": {"anchor_id": "x", "pc": 2, "occurrence": 0}}], output_path="/tmp/hits.json", interval_instructions=20)

    def test_batch_rejects_checkpoint_filename_icount_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_run(command, **kwargs):
                config = Path(command[command.index("--semantic-position") + 1])
                output = Path(next(line.split(" ", 1)[1] for line in config.read_text().splitlines() if line.startswith("output ")))
                output.write_text(json.dumps({"complete": True, "targets": [{"id": 1, "complete": True, "occurrence": 0, "pc": 0x1000, "workload_icount": 10}]}))
                checkpoint = Path(output).parent / "semantic/mcf/1/_9_memory_.zstd"
                checkpoint.parent.mkdir(parents=True)
                checkpoint.write_bytes(b"checkpoint")
                return type("Completed", (), {"returncode": 0})()

            target = {"checkpoint_id": 1, "target": {"build_id": "B", "anchor_id": "B:line", "occurrence": 0, "pc": 0x1000}}
            run_manifest = {"workload_id": "mcf", "build_id": "B", "manifest_sha256": "a" * 64, "interval_instructions": 20}
            with patch("checkpoint_align.position_aligner.materialize.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "filename icount"):
                    run_nemu_targets([target], ["nemu"], run_manifest=run_manifest, output_dir=root / "out")

    def test_batch_ignores_stale_root_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "out" / "semantic" / "mcf" / "1" / "_10_memory_.zstd"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")
            (root / "out" / "semantic-hits.json").write_text(json.dumps({"complete": True, "targets": [{"id": 1, "complete": True, "occurrence": 0, "pc": 0x1000, "workload_icount": 10}]}))

            def fake_run(command, **kwargs):
                return type("Completed", (), {"returncode": 0})()

            target = {"checkpoint_id": 1, "target": {"build_id": "B", "anchor_id": "B:line", "occurrence": 0, "pc": 0x1000}}
            run_manifest = {"workload_id": "mcf", "build_id": "B", "manifest_sha256": "a" * 64, "interval_instructions": 20}
            with patch("checkpoint_align.position_aligner.materialize.subprocess.run", side_effect=fake_run):
                result = run_nemu_targets([target], ["nemu"], run_manifest=run_manifest, output_dir=root / "out")
            self.assertFalse(result["complete"])
            self.assertEqual(result["targets"], {})

    def test_probe_cache_is_bound_to_rng_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nemu = root / "nemu"; nemu.write_bytes(b"nemu")
            firmware = root / "fw"; firmware.write_bytes(b"firmware")

            def fake_run(command, **kwargs):
                config = Path(command[command.index("--semantic-position") + 1])
                output = Path(next(line.split(" ", 1)[1] for line in config.read_text().splitlines() if line.startswith("output ")))
                output.write_text(json.dumps({"schema_version": 2, "mode": "probe-boundary-pcs", "event_phase": "before_instruction", "complete": True, "probes": [{"id": 1, "complete": True}]}))
                return type("Completed", (), {"returncode": 0})()

            point = (CheckpointPoint(1, 0),)
            first = NemuSourceProbeRunner(nemu=nemu, firmware=firmware, output_dir=root / "out", interval_instructions=20, max_instructions=100, rng_seed="0" * 64)
            changed = NemuSourceProbeRunner(nemu=nemu, firmware=firmware, output_dir=root / "out", interval_instructions=20, max_instructions=100, rng_seed="1" * 64)
            with patch("checkpoint_align.position_aligner.source_boundary.subprocess.run", side_effect=fake_run) as run_process:
                first.probe_boundaries(point)
                first.probe_boundaries(point)
                changed.probe_boundaries(point)
            self.assertEqual(run_process.call_count, 2)

    def test_zero_displacement_limit_is_strict(self):
        self.assertEqual(BoundaryPolicy(20, 0).displacement_limit, 0)


if __name__ == "__main__":
    unittest.main()
