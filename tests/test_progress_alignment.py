from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checkpoint_align.cli import bind_source_position, command_calibrate_source_checkpoints, command_checkpoint_all, command_checkpoint_suite, command_collect_events, command_materialize
from checkpoint_align.dwarf_source import AddressRange, Anchor, AnchorCatalog, SourceLocation
from checkpoint_align.position_aligner.materialize import materialization_fingerprint
from checkpoint_align.position_aligner.source_boundary import NemuSourceProbeRunner, ProbeRun


def event(anchor: str, occurrence: int, pc: int, icount: int) -> dict:
    return {"build_id": "A", "run_id": "A-run", "anchor_id": anchor, "semantic_key": anchor.split(":")[-1], "occurrence": occurrence, "pc": pc, "workload_icount": icount, "context": {}}


class SourceBindingTest(unittest.TestCase):
    def test_global_from_scratch_occurrence_binds(self) -> None:
        events = [event("A:loop", 0, 10, 100), event("A:loop", 1, 10, 200)]
        result = bind_source_position(events, {"anchor_id": "A:loop", "occurrence": 1, "occurrence_scope": "global_from_scratch", "requested_icount": 190, "interval_instructions": 100})
        self.assertEqual(result["status"], "snapped")
        self.assertEqual(result["source_position"]["actual_delta_instructions"], 10)

    def test_bare_occurrence_is_not_accepted(self) -> None:
        result = bind_source_position([event("A:loop", 0, 10, 100)], {"anchor_id": "A:loop", "occurrence": 0})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "EVIDENCE_COLLECTION_FAILED")

    def test_request_rejects_per_position_phase(self) -> None:
        with self.assertRaisesRegex(ValueError, "run manifest"):
            bind_source_position([], {"event_phase": "before_instruction"})

    def test_repeated_window_is_ambiguous(self) -> None:
        events = [event("A:loop", 0, 10, 100), event("A:loop", 1, 10, 200)]
        result = bind_source_position(events, {"window_events": [event("A:loop", 0, 10, 0)], "source_event_offset": 0})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "AMBIGUOUS")

    def test_collect_events_requires_nonempty_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_manifest = root / "run.json"
            value = {"schema_version": 1, "workload_id": "fixture", "build_id": "A", "run_id": "fixture-A-run1", "input_id": "input-01", "functional_path_id": "default", "icount_domain": "workload_relative_instructions", "interval_instructions": 100, "interval_index_base": 0, "event_phase": "before_instruction", "elf_sha256": "a" * 64, "manifest_sha256": "a" * 64, "terminal_marker": "done", "deterministic": True, "elf_type": "ET_EXEC", "thread_count": 1, "terminal_observed": False, "runs": []}
            run_manifest.write_text(json.dumps(value))
            trace = root / "trace.json"
            trace.write_text(json.dumps({"build_identity": "a" * 64, "event_phase": "before_instruction", "events": []}))
            output = root / "events.json"
            self.assertEqual(command_collect_events(argparse.Namespace(run_manifest=run_manifest, trace=trace, terminal_marker=None, output=output)), 0)
            self.assertFalse(json.loads(output.read_text())["complete"])


class MaterializeCommandTest(unittest.TestCase):
    def test_zero_runner_exit_without_checkpoint_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alignment = root / "alignment.json"
            run_manifest = root / "run.json"
            alignment.write_text(json.dumps({"status": "matched", "source": {"build_id": "A", "anchor_id": "A:main", "occurrence": 0, "pc": 9}, "semantic_validation": {"status": "validated", "alignment_id": "alignment-1", "source_run_id": "A-run", "target_run_id": "B-run", "source_event": {"anchor_id": "A:main", "occurrence": 0}, "target_event": {"anchor_id": "B:main", "occurrence": 0}, "source_context": {"work_unit": 1, "subphase": "run"}, "target_context": {"work_unit": 1, "subphase": "run"}, "evidence_sha256": "e" * 64}, "correspondence": {"target": {"build_id": "B", "anchor_id": "B:main", "occurrence": 0, "pc": 10}}}))
            run_manifest.write_text(json.dumps({"build_id": "B", "run_id": "B-run", "manifest_sha256": "0" * 64, "interval_instructions": 100, "event_phase": "before_instruction"}))
            args = argparse.Namespace(alignment=alignment, run_manifest=run_manifest, target=root / "target.json", output_dir=root / "output", command=["nemu"])
            with patch("checkpoint_align.cli.run_nemu_target", return_value={"exit_status": 0, "checkpoint_sidecar": None}):
                self.assertEqual(command_materialize(args), 2)


class CheckpointBatchCommandTest(unittest.TestCase):
    @staticmethod
    def envelope(build_id: str, run_id: str, events: list[dict]) -> dict:
        return {
            "evidence_kind": "dynamic_execution",
            "complete": True,
            "run": {
                "schema_version": 1,
                "workload_id": "mcf",
                "build_id": build_id,
                "run_id": run_id,
                "input_id": "test-input",
                "functional_path_id": "test",
                "icount_domain": "workload_relative_instructions",
                "interval_instructions": 100,
                "interval_index_base": 0,
                "event_phase": "before_instruction",
                "elf_sha256": "1" * 64,
                "manifest_sha256": "2" * 64,
                "terminal_marker": "done",
                "deterministic": True,
                "elf_type": "ET_EXEC",
                "thread_count": 1,
            },
            "events": events,
        }

    def test_plan_covers_all_requests_and_keeps_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_events = [event("A:main", 0, 10, 100), event("A:loop", 0, 20, 200)]
            target_events = [event("B:main", 0, 110, 120), event("B:loop", 0, 220, 240)]
            for item in target_events:
                item["build_id"], item["run_id"], item["semantic_key"] = "B", "B-run", item["semantic_key"].split(":", 1)[-1]
            for item in source_events:
                item["build_id"], item["run_id"], item["semantic_key"] = "A", "A-run", item["semantic_key"].split(":", 1)[-1]
            source = root / "source.json"; source.write_text(json.dumps(self.envelope("A", "A-run", source_events)))
            target = root / "target.json"; target.write_text(json.dumps(self.envelope("B", "B-run", target_events)))
            requests = root / "requests.json"; requests.write_text(json.dumps({"checkpoints": [
                {"id": "point-0", "request": {"anchor_id": "A:main", "occurrence": 0, "occurrence_scope": "global_from_scratch", "requested_icount": 100, "interval_instructions": 100}},
                {"id": "point-missing", "request": {"anchor_id": "A:missing", "occurrence": 0, "occurrence_scope": "global_from_scratch", "requested_icount": 0, "interval_instructions": 100}},
            ]}))
            args = argparse.Namespace(source_events=source, target_events=target, requests=requests, correspondence=None, output_dir=root / "out", plan_only=True, resume=False, max_cells=1000, max_seconds=1.0, command=[])
            self.assertEqual(command_checkpoint_all(args), 2)
            result = json.loads((root / "out" / "checkpoint-all-result.json").read_text())
            self.assertEqual(result["counts"]["requested"], 2)
            self.assertEqual(result["counts"]["candidate"], 1)
            self.assertEqual(result["counts"]["rejected"], 1)
            self.assertEqual(result["items"][0]["status"], "candidate")
            self.assertEqual(result["items"][1]["reason"], "EVIDENCE_COLLECTION_FAILED")

    def test_plan_accepts_exact_boundary_prealigned_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_checkpoint = root / "source.zstd"; source_checkpoint.write_bytes(b"checkpoint")
            target_run = self.envelope("B", "B-run", [])["run"]
            requests = root / "requests.json"
            requests.write_text(json.dumps({"target_run": target_run, "checkpoints": [{
                "id": "point-7", "checkpoint_id": 7, "source_checkpoint": str(source_checkpoint),
                "source_position": {"build_id": "A", "anchor_id": "A:line", "occurrence": 3, "pc": 0x1000, "workload_icount": 95},
                "target_position": {"build_id": "B", "anchor_id": "B:line", "occurrence": 3, "pc": 0x2000},
                "position_status": "snapped", "alignment_evidence": {"method": "exact_boundary_source_marker_occurrence"},
            }]}))
            args = argparse.Namespace(source_events=None, target_events=None, run_manifest=None, requests=requests, correspondence=None, output_dir=root / "out", plan_only=True, resume=False, max_cells=1000, max_seconds=1.0, command=[])
            self.assertEqual(command_checkpoint_all(args), 2)
            result = json.loads((root / "out/checkpoint-all-result.json").read_text())
            self.assertEqual(result["items"][0]["status"], "candidate")

    def test_exact_boundary_request_writes_sidecar_from_actual_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_checkpoint = root / "source.zstd"; source_checkpoint.write_bytes(b"source")
            target_checkpoint = root / "target.zstd"; target_checkpoint.write_bytes(b"target")
            target_run = self.envelope("B", "B-run", [])["run"]
            requests = root / "requests.json"
            requests.write_text(json.dumps({"target_run": target_run, "checkpoints": [{
                "id": "point-7", "checkpoint_id": 7, "source_checkpoint": str(source_checkpoint),
                "source_position": {"build_id": "A", "anchor_id": "A:line", "occurrence": 3, "pc": 0x1000, "workload_icount": 95},
                "target_position": {"build_id": "B", "anchor_id": "B:line", "occurrence": 3, "pc": 0x2000},
                "position_status": "snapped", "alignment_evidence": {"method": "exact_boundary_source_marker_occurrence"}, "semantic_validation": {"status": "validated", "alignment_id": "alignment-1", "source_run_id": "A-run", "target_run_id": "B-run", "source_event": {"anchor_id": "A:line", "occurrence": 3}, "target_event": {"anchor_id": "B:line", "occurrence": 3}, "source_context": {"work_unit": 1, "subphase": "run"}, "target_context": {"work_unit": 1, "subphase": "run"}, "evidence_sha256": "e" * 64},
            }]}))
            materialized = {"exit_status": 0, "targets": {"7": {"hit": {"occurrence": 3, "pc": 0x2000, "workload_icount": 123}, "checkpoint": str(target_checkpoint), "checkpoint_sha256": "f" * 64, "run_fingerprint": "f" * 64}}}
            args = argparse.Namespace(source_events=None, target_events=None, run_manifest=None, requests=requests, correspondence=None, output_dir=root / "out", plan_only=False, resume=False, max_cells=1000, max_seconds=1.0, command=["nemu"])
            with patch("checkpoint_align.cli.run_nemu_targets", return_value=materialized):
                self.assertEqual(command_checkpoint_all(args), 0)
            sidecar = json.loads((root / "out/point-7/materialization/checkpoint-sidecar.json").read_text())
            self.assertEqual(sidecar["workload_icount"], 123)
            self.assertEqual(sidecar["checkpoint"], str(target_checkpoint))

    def test_resume_reuses_existing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_event = event("A:main", 0, 10, 100); source_event.update(build_id="A", run_id="A-run", semantic_key="main")
            target_event = event("B:main", 0, 110, 120); target_event.update(build_id="B", run_id="B-run", semantic_key="main")
            source = root / "source.json"; source.write_text(json.dumps(self.envelope("A", "A-run", [source_event])))
            target = root / "target.json"; target.write_text(json.dumps(self.envelope("B", "B-run", [target_event])))
            requests = root / "requests.json"; requests.write_text(json.dumps({"checkpoints": [{"id": "point-0", "request": {"anchor_id": "A:main", "occurrence": 0, "occurrence_scope": "global_from_scratch", "requested_icount": 100, "interval_instructions": 100}, "semantic_validation": {"status": "validated", "alignment_id": "alignment-1", "source_run_id": "A-run", "target_run_id": "B-run", "source_event": {"anchor_id": "A:main", "occurrence": 0}, "target_event": {"anchor_id": "B:main", "occurrence": 0}, "source_context": {"work_unit": 1, "subphase": "run"}, "target_context": {"work_unit": 1, "subphase": "run"}, "evidence_sha256": "e" * 64}}]}))
            output = root / "out"
            args = argparse.Namespace(source_events=source, target_events=target, requests=requests, correspondence=None, output_dir=output, plan_only=False, resume=True, max_cells=1000, max_seconds=1.0, command=["nemu"])
            with patch("checkpoint_align.cli.run_nemu_target", return_value={"exit_status": 0, "checkpoint_sidecar": str(output / "point-0" / "materialization" / "checkpoint-sidecar.json")}):
                sidecar_dir = output / "point-0" / "materialization"; sidecar_dir.mkdir(parents=True)
                checkpoint = sidecar_dir / "x_memory_.zstd"; checkpoint.write_bytes(b"checkpoint")
                sidecar = {"build_id": "B", "run_id": "B-run", "run_manifest_sha256": "2" * 64, "run_fingerprint": materialization_fingerprint(self.envelope("B", "B-run", [target_event])["run"], ["nemu"], {"build_id": "B", "anchor_id": "B:main", "occurrence": 0, "pc": 110}), "anchor_id": "B:main", "occurrence": 0, "pc": 110, "checkpoint": str(checkpoint), "checkpoint_sha256": hashlib.sha256(b"checkpoint").hexdigest()}
                (sidecar_dir / "checkpoint-sidecar.json").write_text(json.dumps(sidecar))
                self.assertEqual(command_checkpoint_all(args), 0)
            result = json.loads((output / "checkpoint-all-result.json").read_text())
            self.assertEqual(result["items"][0]["status"], "reused")

    def test_calibrate_source_checkpoint_builds_dynamic_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "point" / "7" / "_7_memory_.zstd"
            checkpoint.parent.mkdir(parents=True); checkpoint.write_bytes(b"A checkpoint")
            source_event = event("A:main", 3, 110, 0); source_event.update(build_id="A", run_id="A-run", semantic_key="main")
            target_event = event("B:main", 3, 210, 0); target_event.update(build_id="B", run_id="B-run", semantic_key="main")
            source = root / "source.json"; source.write_text(json.dumps(self.envelope("A", "A-run", [source_event])))
            watchlist = root / "watchlist.json"; watchlist.write_text(json.dumps({"watches": {"0": {"pc": 110, "semantic_key": "main", "anchor_id": "A:main"}}}))

            source_run_value = self.envelope("A", "A-run", [source_event])["run"]; source_run_value["interval_instructions"] = 20_000_000
            target_run_value = self.envelope("B", "B-run", [target_event])["run"]; target_run_value["interval_instructions"] = 20_000_000
            source_run = root / "source-run.json"; source_run.write_text(json.dumps(source_run_value))
            target_run = root / "target-run.json"; target_run.write_text(json.dumps(target_run_value))
            source_trace = root / "source.u32"; source_trace.write_bytes((0).to_bytes(4, "little"))
            target_trace = root / "target.u32"; target_trace.write_bytes((0).to_bytes(4, "little"))
            target_watchlist = root / "target-watchlist.json"; target_watchlist.write_text(json.dumps({"watches": {"0": {"pc": 210, "semantic_key": "main", "anchor_id": "B:main"}}}))
            for build, pc, path in (("A", 0x1000, root / "source-catalog.json"), ("B", 0x2000, root / "target-catalog.json")):
                anchor = Anchor(f"{build}:fn", "function", "fn", None, SourceLocation("src/a.c", 7, 2, 0), (AddressRange(pc, pc + 32),), "M", "fixture")
                catalog = AnchorCatalog(build, "a" * 64, build, build, None, "RISC-V", 0, 1, (anchor,), (), ({"pc": pc, "source": {"path": "src/a.c", "line": 7, "column": 2, "discriminator": 0}},), (), {}, {}, {})
                catalog.save(path)
            args = argparse.Namespace(source_checkpoints=root / "point", source_run_manifest=source_run, target_run_manifest=target_run, source_watchlist_manifest=watchlist, target_watchlist_manifest=target_watchlist, source_firmware=root / "fw", nemu=root / "nemu", output_dir=root / "calibration", output=root / "requests.json", workload_id="mcf", source_build_id="A", target_build_id="B", interval_instructions=20_000_000, warmup_instructions=20_000_000, boot_allowance=200_000_000, tail_instructions=20_000_000, max_instructions=40_000_000, max_source_displacement=25_000_000, force_probe=False, rng_seed="0" * 64, context_size=32, timeout=10)
            boundary_run = ProbeRun({"schema_version": 2, "mode": "probe-boundary-pcs", "event_phase": "before_instruction", "complete": True, "probes": [{"id": 7, "requested_icount": 120_000_000, "observed_icount": 120_000_000, "pc": 0x1000, "complete": True}]}, "/boundary.json", "b" * 64)
            occurrence_run = ProbeRun({"schema_version": 2, "mode": "probe-boundary-occurrences", "event_phase": "before_instruction", "complete": True, "probes": [{"id": 7, "requested_icount": 120_000_000, "boundary_pc": 0x1000, "marker_pc": 0x1000, "marker_hit_icount": 140_000_001, "occurrence": 3, "complete": True}]}, "/occurrence.json", "o" * 64)
            with patch.object(NemuSourceProbeRunner, "probe_boundaries", return_value=boundary_run), patch.object(NemuSourceProbeRunner, "probe_occurrences", return_value=occurrence_run):
                self.assertEqual(command_calibrate_source_checkpoints(args), 0)
            requests = json.loads(args.output.read_text())["checkpoints"]
            self.assertEqual(requests[0]["source_position"]["occurrence"], 3)
            self.assertEqual(requests[0]["alignment_evidence"]["method"], "exact_boundary_source_marker_occurrence")
            self.assertEqual(requests[0]["semantic_validation"]["status"], "candidate")
            self.assertEqual(requests[0]["alignment_evidence"]["rng_seed"], "0" * 64)
            self.assertEqual(json.loads((root / "calibration/source-resolution.json").read_text())["rng_seed"], "0" * 64)

    def test_checkpoint_suite_passes_rng_seed_to_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite = root / "suite"
            for build in ("A", "B"):
                for relative in (Path("elf/mcf.elf"), Path("bin/mcf.fw_payload.bin")):
                    path = suite / "workloads/mcf" / build / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fixture")
            nemu = root / "nemu"; nemu.write_bytes(b"nemu")
            source_checkpoints = root / "checkpoints"; source_checkpoints.mkdir()
            args = argparse.Namespace(suite=suite, workload_id="mcf", source_label="A", target_label="B", source_checkpoints=source_checkpoints, nemu=nemu, output_dir=root / "out", anchor_name=[], interval_instructions=20, warmup_instructions=20, boot_allowance=200, tail_instructions=20, max_instructions=100, max_source_displacement=20, force_probe=False, rng_seed="1" * 64, timeout=10, plan_only=True, resume=False)
            captured = {}

            def calibrate(calibration_args):
                captured["rng_seed"] = calibration_args.rng_seed
                return 0

            with patch("checkpoint_align.cli.command_prepare_watchlists", return_value=0), patch("checkpoint_align.cli.command_calibrate_source_checkpoints", side_effect=calibrate), patch("checkpoint_align.cli.command_checkpoint_all", return_value=0):
                self.assertEqual(command_checkpoint_suite(args), 0)
            self.assertEqual(captured["rng_seed"], "1" * 64)


if __name__ == "__main__":
    unittest.main()
