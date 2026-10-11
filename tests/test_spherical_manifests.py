from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from open_storyline.spherical.manifests import (
    canonical_json_bytes,
    export_json_schemas,
    PUBLIC_SCHEMA_MODELS,
    sha256_file,
    sha256_json,
)


class SphericalManifestTests(unittest.TestCase):
    def test_canonical_json_hash_is_order_independent(self) -> None:
        left = {"track": {"yaw": 10, "pitch": 5}, "hash": "abc"}
        right = {"hash": "abc", "track": {"pitch": 5, "yaw": 10}}
        self.assertEqual(canonical_json_bytes(left), canonical_json_bytes(right))
        self.assertEqual(sha256_json(left), sha256_json(right))

    def test_streaming_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "data.bin"
            path.write_bytes(b"openstoryline-spherical")
            self.assertEqual(
                sha256_file(path),
                "d76bd28fe40ded5a2b3164e308375938689037d0e420222b05b9b1233043021e",
            )

    def test_json_schema_export_matches_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            paths = export_json_schemas(temporary_name)
            self.assertEqual(len(paths), len(PUBLIC_SCHEMA_MODELS))
            for name, model_type in PUBLIC_SCHEMA_MODELS.items():
                payload = json.loads(
                    (Path(temporary_name) / f"{name}.schema.json").read_text()
                )
                self.assertEqual(payload, model_type.model_json_schema())


if __name__ == "__main__":
    unittest.main()
