#!/usr/bin/env python3
"""SDK-independent protocol tests for the public C++ worker build."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


WORKER = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
sys.argv = [sys.argv[0]]


class WorkerCliTests(unittest.TestCase):
    def run_worker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        assert WORKER is not None
        return subprocess.run(
            [str(WORKER), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_capabilities_are_machine_readable_and_truthful(self) -> None:
        completed = self.run_worker("capabilities")
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["provider"], "stub")
        self.assertFalse(payload["sdk_enabled"])
        self.assertFalse(payload["can_decode_insv"])
        self.assertFalse(payload["can_stitch"])
        self.assertIn("cannot decode .insv", " ".join(payload["warnings"]))
        self.assertIn("command=capabilities", completed.stderr)

    def test_discovery_proposes_but_never_silently_stitches(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-discovery-") as name:
            root = Path(name) / "素材"
            root.mkdir()
            front = root / "VID_20260724_120000_00_001.insv"
            back = root / "VID_20260724_120000_10_001.insv"
            single = root / "X4_single_track.insv"
            marker_single = root / "VID_20260721_180140_00_036.insv"
            front.write_bytes(b"a" * 100)
            back.write_bytes(b"b" * 95)
            single.write_bytes(b"c" * 50)
            marker_single.write_bytes(b"d" * 60)
            timestamp = 1_800_000_000
            os.utime(front, (timestamp, timestamp))
            os.utime(back, (timestamp + 1, timestamp + 1))

            completed = self.run_worker(
                "discover",
                "--directory",
                str(root),
            )

            self.assertEqual(completed.returncode, 0)
            payload = json.loads(completed.stdout)
            self.assertEqual(len(payload["groups"]), 3)
            pair = next(group for group in payload["groups"] if len(group["files"]) == 2)
            self.assertEqual(
                [Path(path).name for path in pair["proposed_order"]],
                [front.name, back.name],
            )
            self.assertGreaterEqual(pair["confidence"], 0.9)
            self.assertTrue(pair["requires_confirmation"])
            singleton = next(
                group
                for group in payload["groups"]
                if [Path(path).name for path in group["proposed_order"]]
                == [single.name]
            )
            self.assertLess(singleton["confidence"], pair["confidence"])
            self.assertTrue(singleton["warnings"])
            ambiguous_marker_singleton = next(
                group
                for group in payload["groups"]
                if [Path(path).name for path in group["proposed_order"]]
                == [marker_single.name]
            )
            self.assertEqual(
                ambiguous_marker_singleton["files"][0]["lens_marker"], "00"
            )
            self.assertLess(
                ambiguous_marker_singleton["confidence"], singleton["confidence"]
            )
            self.assertIn(
                "ambiguous",
                " ".join(ambiguous_marker_singleton["warnings"]).lower(),
            )
            self.assertTrue(ambiguous_marker_singleton["requires_confirmation"])
            self.assertEqual(payload["ungrouped"], [])

    def test_probe_reports_file_metadata_without_claiming_decode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-probe-") as name:
            source = Path(name) / "camera.insv"
            source.write_bytes(b"raw-placeholder")
            completed = self.run_worker("probe", "--input", str(source))
            self.assertEqual(completed.returncode, 0)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["decodable"])
            self.assertEqual(payload["inputs"][0]["size_bytes"], 15)
            self.assertEqual(payload["inputs"][0]["extension"], ".insv")

    def test_probe_rejects_non_insv_input_before_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-probe-invalid-") as name:
            source = Path(name) / "ordinary.mp4"
            source.write_bytes(b"not raw camera media")
            completed = self.run_worker("probe", "--input", str(source))
            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["event"], "error")
            self.assertEqual(payload["code"], "invalid_arguments")

    def test_stub_stitch_emits_progress_then_structured_capability_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-stitch-") as name:
            root = Path(name)
            source = root / "camera.insv"
            source.write_bytes(b"raw-placeholder")
            output = root / "master.mp4"
            request = {
                "schema_version": "1.0",
                "job_id": "12345678-1234-4234-9234-123456789abc",
                "input_paths": [str(source)],
                "output_path": str(output),
                "output_width": 640,
                "output_height": 320,
                "codec": "h264",
                "bitrate_bps": 10_000_000,
                "enable_flowstate": True,
                "enable_direction_lock": False,
                "stitch_type": "template",
                "enable_stitch_fusion": False,
                "model_root": None,
                "overwrite": False,
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")

            completed = self.run_worker(
                "stitch",
                "--request",
                str(request_path),
            )

            self.assertEqual(completed.returncode, 4)
            events = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(events[0]["event"], "progress")
            self.assertEqual(events[0]["progress_percent"], 0)
            self.assertEqual(events[-1]["event"], "error")
            self.assertEqual(events[-1]["code"], "sdk_unavailable")
            self.assertEqual(events[-1]["job_id"], request["job_id"])
            self.assertFalse(output.exists())

    def test_invalid_dimensions_fail_before_provider_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-invalid-") as name:
            root = Path(name)
            source = root / "camera.insv"
            source.write_bytes(b"raw-placeholder")
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "job_id": "12345678-1234-4234-9234-123456789abc",
                        "input_paths": [str(source)],
                        "output_path": str(root / "master.mp4"),
                        "output_width": 640,
                        "output_height": 321,
                    }
                ),
                encoding="utf-8",
            )
            completed = self.run_worker("stitch", "--request", str(request_path))
            self.assertEqual(completed.returncode, 3)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["event"], "error")
            self.assertEqual(payload["code"], "invalid_dimensions")


if __name__ == "__main__":
    unittest.main()
