from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError, Repository, prepare_environment


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspaces"
        self.checkout = self.workspace / "Example"
        self.checkout.mkdir(parents=True)
        (self.checkout / "environment.yml").write_text("name: example\n")
        self.environment_root = self.workspace / ".environments"
        self.prefix = self.environment_root / "Example"
        self.repository = Repository(
            name="Example",
            url="https://example.invalid/example.git",
            revision="main",
            commit="0" * 40,
            directory="Example",
            environment_file="environment.yml",
            install_extras="dev",
            import_name="example_package",
            validation_imports=("pkg_resources", "uproot"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_installs_and_then_reuses_environment(self) -> None:
        calls: list[tuple[list[str], Path | None]] = []

        def fake_run(arguments: list[str], cwd: Path | None = None) -> str:
            calls.append((list(arguments), cwd))
            if arguments[:3] == ["/tools/mamba", "env", "create"]:
                python = self.prefix / "bin/python"
                python.parent.mkdir(parents=True)
                python.write_text("")
            if arguments[1:2] == ["-c"]:
                return str(self.checkout / "example_package/__init__.py")
            return "ok"

        with patch("workflow_automation.cli.shutil.which", return_value="/tools/mamba"), patch(
            "workflow_automation.cli.run_program", side_effect=fake_run
        ):
            prepare_environment(self.repository, self.workspace, self.environment_root)
            prepare_environment(self.repository, self.workspace, self.environment_root)

        create_calls = [call for call, _ in calls if call[:3] == ["/tools/mamba", "env", "create"]]
        install_calls = [call for call, _ in calls if call[1:4] == ["-m", "pip", "install"]]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(len(install_calls), 1)
        self.assertEqual(install_calls[0][-1], ".[dev]")
        validation_commands = [call for call, _ in calls if call[1:2] == ["-c"]]
        self.assertTrue(any(call[-1] == "import pkg_resources" for call in validation_commands))
        self.assertTrue(any(call[-1] == "import uproot" for call in validation_commands))

    def test_refuses_incomplete_existing_prefix(self) -> None:
        self.prefix.mkdir(parents=True)

        with self.assertRaisesRegex(BootstrapError, "not a usable environment"):
            prepare_environment(self.repository, self.workspace, self.environment_root)

    def test_requires_environment_creator(self) -> None:
        with patch("workflow_automation.cli.shutil.which", return_value=None):
            with self.assertRaisesRegex(BootstrapError, "mamba or conda"):
                prepare_environment(self.repository, self.workspace, self.environment_root)


if __name__ == "__main__":
    unittest.main()
