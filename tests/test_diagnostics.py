from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import collect_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_missing_workspace_is_reported_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "missing"
            config = root / "repositories.json"
            config.write_text(
                json.dumps(
                    {
                        "repositories": {
                            "example": {
                                "url": "https://example.invalid/repository.git",
                                "revision": "main",
                            }
                        }
                    }
                )
            )

            diagnostics = collect_diagnostics(config, workspace)

            self.assertTrue(diagnostics["read_only"])
            self.assertFalse(diagnostics["workspace"]["exists"])
            self.assertFalse(workspace.exists())
            self.assertEqual(diagnostics["repositories"][0]["exists"], False)

    def test_environment_values_are_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing_config = root / "missing.json"
            secret = "do-not-print-this-value"
            with patch.dict(
                os.environ,
                {"CONDOR_CONFIG": secret, "X509_USER_PROXY": str(root / "proxy")},
                clear=False,
            ):
                diagnostics = collect_diagnostics(missing_config, root)

            serialized = json.dumps(diagnostics)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(str(root / "proxy"), serialized)
            self.assertTrue(diagnostics["environment"]["CONDOR_CONFIG"]["set"])
            self.assertTrue(diagnostics["environment"]["X509_USER_PROXY"]["set"])

    def test_invalid_config_is_returned_as_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "repositories.json"
            config.write_text("not json")

            diagnostics = collect_diagnostics(config, root)

            self.assertIn("configuration_error", diagnostics)
            self.assertEqual(diagnostics["repositories"], [])


if __name__ == "__main__":
    unittest.main()
