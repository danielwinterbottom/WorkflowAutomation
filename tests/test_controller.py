from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError, prepare_controller


class ControllerTests(unittest.TestCase):
    def test_creates_installs_and_then_reuses_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prefix = Path(temporary_directory) / ".venv"
            calls: list[list[str]] = []

            class FakeBuilder:
                def __init__(self, with_pip: bool) -> None:
                    self.with_pip = with_pip

                def create(self, destination: Path) -> None:
                    self.assert_with_pip()
                    python = destination / "bin/python"
                    python.parent.mkdir(parents=True)
                    python.write_text("")

                def assert_with_pip(self) -> None:
                    if not self.with_pip:
                        raise AssertionError("controller must include pip")

            def fake_run(arguments: list[str], cwd: Path | None = None) -> str:
                calls.append(list(arguments))
                return ""

            with patch("workflow_automation.cli.venv.EnvBuilder", FakeBuilder), patch(
                "workflow_automation.cli.run_program", side_effect=fake_run
            ):
                prepare_controller(prefix)
                prepare_controller(prefix)

            installs = [call for call in calls if call[1:4] == ["-m", "pip", "install"]]
            self.assertEqual(len(installs), 1)
            self.assertIn("--editable", installs[0])

    def test_refuses_non_environment_controller_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prefix = Path(temporary_directory) / ".venv"
            prefix.mkdir()

            with self.assertRaisesRegex(BootstrapError, "not a usable Python environment"):
                prepare_controller(prefix)


if __name__ == "__main__":
    unittest.main()
