from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from open_storyline.config import SphericalConfig, load_settings
from open_storyline.spherical import (
    CURRENT_SCHEMA_VERSION,
    SphericalDisabledError,
    VersionedSphericalModel,
)


class SphericalScaffoldingTests(unittest.TestCase):
    def test_spherical_feature_flags_are_disabled_by_default(self) -> None:
        config = SphericalConfig()

        self.assertFalse(config.enabled)
        self.assertFalse(config.enable_sdk_worker)

    def test_repository_config_keeps_extension_disabled(self) -> None:
        settings = load_settings("config.toml")

        self.assertFalse(settings.spherical.enabled)
        self.assertFalse(settings.spherical.enable_sdk_worker)

    def test_versioned_contract_is_strict_and_emits_json_schema(self) -> None:
        model = VersionedSphericalModel()

        self.assertEqual(model.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            VersionedSphericalModel.model_json_schema()["properties"][
                "schema_version"
            ]["const"],
            CURRENT_SCHEMA_VERSION,
        )
        with self.assertRaises(ValidationError):
            VersionedSphericalModel.model_validate(
                {"schema_version": "1.0", "unexpected": True}
            )

    def test_old_config_without_spherical_block_uses_safe_defaults(self) -> None:
        original = Path("config.toml").read_text(encoding="utf-8")
        without_block = original.replace(
            "# =========== optional spherical media support ===========\n"
            "[spherical]\n"
            "enabled = false\n"
            "enable_sdk_worker = false\n"
            "workspace_dir = \"./outputs\"\n"
            "cache_dir = \"./.storyline/spherical-cache\"\n"
            "renderer_sample_interval_seconds = 0.25\n"
            "proxy_width = 1024\n"
            "contact_sheet_thumbnail_width = 320\n"
            "preview_max_dimension = 640\n\n",
            "",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(without_block, encoding="utf-8")
            settings = load_settings(config_path)

        self.assertFalse(settings.spherical.enabled)
        self.assertFalse(settings.spherical.enable_sdk_worker)

    def test_typed_error_has_stable_transport_form(self) -> None:
        error = SphericalDisabledError(
            "Spherical support is disabled.",
            details={"feature": "spherical"},
        )

        self.assertEqual(
            error.as_dict(),
            {
                "code": "spherical_disabled",
                "message": "Spherical support is disabled.",
                "details": {"feature": "spherical"},
            },
        )


if __name__ == "__main__":
    unittest.main()
