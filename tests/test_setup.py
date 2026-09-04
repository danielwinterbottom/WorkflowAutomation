from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import (
    BootstrapError,
    Repository,
    environment_validation_error,
    prepare_environment,
)


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
            pip_install_dependencies=False,
            import_name="example_package",
            validation_imports=("pkg_resources", "uproot"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_installs_and_then_reuses_environment(self) -> None:
        calls: list[tuple[list[str], Path | None]] = []

        def fake_run(
            arguments: list[str], cwd: Path | None = None, env: dict | None = None
        ) -> str:
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
        self.assertIn("--no-deps", install_calls[0])
        self.assertIn("--no-build-isolation", install_calls[0])
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

    def test_validation_names_the_failed_import(self) -> None:
        python = self.prefix / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("")

        def fake_run(
            arguments: list[str], cwd: Path | None = None, env: dict | None = None
        ) -> str:
            if arguments[-1] == "import pkg_resources":
                raise BootstrapError("pkg_resources is missing")
            if arguments[1:2] == ["-c"]:
                return str(self.checkout / "example_package/__init__.py")
            return ""

        with patch("workflow_automation.cli.run_program", side_effect=fake_run):
            error = environment_validation_error(self.repository, self.checkout, self.prefix)

        self.assertIn("validation module pkg_resources", error or "")


if __name__ == "__main__":
    unittest.main()


class RuntimeEnvironmentTests(unittest.TestCase):
    """Repositories that need an external installation, such as ROOT."""

    def repository(self, **kwargs):
        from workflow_automation.cli import Repository
        defaults = dict(
            name="TIDAL", url="git@example:TIDAL.git", revision="workflowautomation",
            commit="a" * 40, directory="TIDAL",
        )
        defaults.update(kwargs)
        return Repository(**defaults)

    def test_declared_variables_are_prepended_not_replaced(self):
        # An external ROOT has to sit alongside the environment's own Python,
        # so its paths are prepended rather than overwriting what is there.
        from workflow_automation.cli import runtime_environment
        repo = self.repository(environment_variables=(
            ("ROOTSYS", "/cvmfs/root"), ("PYTHONPATH", "/cvmfs/root/lib"),
        ))
        with patch.dict("os.environ", {"PYTHONPATH": "/existing"}, clear=False):
            env = runtime_environment(repo, Path("/prefix"))
        self.assertEqual(env["ROOTSYS"], "/cvmfs/root")
        self.assertEqual(env["PYTHONPATH"].split(":"), ["/cvmfs/root/lib", "/existing"])

    def test_the_environment_python_comes_first_on_path(self):
        from workflow_automation.cli import runtime_environment
        env = runtime_environment(self.repository(), Path("/prefix"))
        self.assertEqual(env["PATH"].split(":")[0], "/prefix/bin")

    def test_a_build_that_produces_nothing_is_an_error(self):
        # Reporting it here names the build; letting it through would surface
        # later as an unexplained import failure.
        from workflow_automation.cli import run_build_commands
        from workflow_automation.cli import BootstrapError as Error
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            repo = self.repository(
                build_commands=(("Draw", ("true",)),),
                build_artefacts=("Draw/lib/libMultiDraw.so",),
            )
            (checkout / "Draw").mkdir()
            with patch("workflow_automation.cli.run_program", return_value=""):
                with self.assertRaisesRegex(Error, "did not produce"):
                    run_build_commands(repo, checkout, Path("/prefix"))

    def test_a_missing_artefact_invalidates_the_environment(self):
        from workflow_automation.cli import environment_validation_error
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            prefix = checkout / "env"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "bin" / "python").write_text("")
            repo = self.repository(
                import_name="Draw", build_artefacts=("Draw/lib/libMultiDraw.so",),
            )
            with patch("workflow_automation.cli.run_program", return_value=str(checkout / "Draw/__init__.py")):
                error = environment_validation_error(repo, checkout, prefix)
            self.assertIn("build artefact is missing", error)
