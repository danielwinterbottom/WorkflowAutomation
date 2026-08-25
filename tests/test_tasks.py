import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import Repository
from workflow_automation.tasks import (
    DitauInputPreparation,
    DitauProductionPlan,
    DitauSampleManifest,
    GridCredentialCheck,
    RepositoryCheckout,
    RepositoryEnvironment,
)


class RepositoryEnvironmentTests(unittest.TestCase):
    def test_is_incomplete_when_checkout_is_not_current(self):
        task = RepositoryEnvironment(repository="HiggsDNA")

        with patch.object(RepositoryCheckout, "complete", return_value=False), patch(
            "workflow_automation.tasks.validate_environment", return_value=True
        ):
            self.assertFalse(task.complete())


class GridCredentialCheckTests(unittest.TestCase):
    def test_is_incomplete_when_tool_is_missing(self):
        with patch("workflow_automation.tasks.shutil.which", return_value=None):
            self.assertFalse(GridCredentialCheck().complete())


class DitauInputPreparationTests(unittest.TestCase):
    def test_requires_plan_and_one_manifest_for_2022_test(self):
        requirements = DitauInputPreparation().requires()

        self.assertIsInstance(requirements["plan"], DitauProductionPlan)
        self.assertEqual(len(requirements["sample_manifests"]), 1)
        manifest = requirements["sample_manifests"][0]
        self.assertIsInstance(manifest, DitauSampleManifest)
        self.assertEqual(manifest.era, "Run3_2022")


class DitauSampleManifestTests(unittest.TestCase):
    def test_run_uses_strict_external_output_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = root / "HiggsDNA"
            checkout.mkdir()
            productions = root / "productions.json"
            productions.write_text(
                json.dumps(
                    {
                        "productions": {
                            "test": {
                                "analysis_type": "cp",
                                "eras": ["Run3_2022"],
                                "channels": ["tt", "et", "mt"],
                            }
                        }
                    }
                )
            )
            repository = Repository(
                name="HiggsDNA",
                url="https://example.invalid/HiggsDNA.git",
                revision="workflowautomation",
                commit="a" * 40,
                directory="HiggsDNA",
            )
            task = DitauSampleManifest(
                production="test",
                era="Run3_2022",
                productions_config=str(productions),
                workspace=str(root),
            )
            calls = []

            def fake_run(arguments, cwd=None):
                calls.append((arguments, cwd))
                output_dir = Path(arguments[arguments.index("--output-dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                for name in task.expected_names():
                    (output_dir / name).write_text("{}\n")
                return ""

            with patch.object(task, "checkout", return_value=(repository, checkout)), patch.object(
                task, "current_fingerprint", return_value="fingerprint"
            ), patch("workflow_automation.tasks.run_git", return_value=repository.commit), patch(
                "workflow_automation.tasks.run_program", side_effect=fake_run
            ):
                task.run()

            arguments, cwd = calls[0]
            self.assertEqual(cwd, checkout)
            self.assertIn("--strict", arguments)
            self.assertEqual(
                Path(arguments[arguments.index("--output-dir") + 1]), task.sample_dir()
            )
            receipt = json.loads(Path(task.output().path).read_text())
            self.assertEqual(receipt["input_fingerprint"], "fingerprint")
            self.assertEqual(sorted(receipt["files"]), sorted(task.expected_names()))


class DitauProductionPlanTests(unittest.TestCase):
    def test_requires_higgsdna_environment(self):
        requirement = DitauProductionPlan().requires()

        self.assertIsInstance(requirement, RepositoryEnvironment)
        self.assertEqual(requirement.repository, "HiggsDNA")

    def test_is_incomplete_when_environment_is_invalid(self):
        task = DitauProductionPlan()

        with patch.object(RepositoryEnvironment, "complete", return_value=False):
            self.assertFalse(task.complete())


if __name__ == "__main__":
    unittest.main()
