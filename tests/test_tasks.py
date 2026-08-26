import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError, Repository
from workflow_automation.tasks import (
    DitauInputPreparation,
    DitauEffectiveEventPlan,
    DitauEffectiveEventReadiness,
    DitauEffectiveEventSubmission,
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
                                "input_snapshot": "test-snapshot",
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
                    (output_dir / name).write_text(
                        '{"sample": ["root://example.invalid/nano.root"]}\n'
                    )
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

    def test_rejects_null_sample_file_list(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "samples_MC.json"
            path.write_text('{"broken-sample": null}\n')

            with self.assertRaisesRegex(BootstrapError, "non-empty list of files"):
                DitauSampleManifest.validate_sample_file(path)


class DitauEffectiveEventPlanTests(unittest.TestCase):
    def test_generates_two_non_submitting_tree_commands(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = root / "HiggsDNA"
            base_config = checkout / "scripts/ditau/config/ditau_analysis.json"
            base_config.parent.mkdir(parents=True)
            base_config.write_text(
                json.dumps(
                    {
                        "samplejson": "old.json",
                        "year": "old",
                        "Run_Effective": False,
                        "EventsNotSelected": False,
                    }
                )
            )
            repositories = root / "repositories.json"
            repositories.write_text(
                json.dumps(
                    {
                        "repositories": {
                            "HiggsDNA": {
                                "url": "https://example.invalid/HiggsDNA.git",
                                "revision": "workflowautomation",
                                "commit": "a" * 40,
                                "directory": "HiggsDNA",
                            }
                        }
                    }
                )
            )
            productions = root / "productions.json"
            productions.write_text(
                json.dumps(
                    {
                        "productions": {
                            "test": {
                                "analysis_type": "cp",
                                "eras": ["Run3_2022"],
                                "channels": ["tt", "et", "mt"],
                                "effective_output": "output/effective/test",
                            }
                        }
                    }
                )
            )
            task = DitauEffectiveEventPlan(
                production="test",
                era="Run3_2022",
                config=str(repositories),
                productions_config=str(productions),
                workspace=str(root),
            )
            receipt = task.sample_receipt()
            receipt.parent.mkdir(parents=True)
            receipt.write_text('{"receipt": true}\n')

            with patch("workflow_automation.tasks.run_git", return_value="a" * 40):
                task.run()

            plan = json.loads(Path(task.output().path).read_text())
            self.assertFalse(plan["submission_enabled"])
            self.assertEqual(
                [command["tree"] for command in plan["commands"]],
                ["Events", "EventsNotSelected"],
            )
            self.assertEqual(
                set(plan["analysis_configs"]), {"Events.json", "EventsNotSelected.json"}
            )
            self.assertTrue(all(command["submits_jobs"] for command in plan["commands"]))
            for command in plan["commands"]:
                self.assertIn("--submission-manifest-dir", command["argv"])
                self.assertNotIn("condor_submit", command["argv"])
                channel_index = command["argv"].index("--channel")
                self.assertEqual(command["argv"][channel_index + 1], "tt")
            for tree, expected in (("Events", False), ("EventsNotSelected", True)):
                analysis = json.loads(
                    (task.state_dir() / "analysis-configs" / f"{tree}.json").read_text()
                )
                self.assertTrue(analysis["Run_Effective"])
                self.assertEqual(analysis["EventsNotSelected"], expected)
                self.assertTrue(analysis["samplejson"].endswith("samples_MC.json"))

            with patch.object(
                DitauInputPreparation, "complete", return_value=True
            ), patch.object(task, "current_fingerprint", return_value=plan["input_fingerprint"]):
                self.assertTrue(task.complete())
                events = task.state_dir() / "analysis-configs/Events.json"
                events.write_text("{}\n")
                self.assertFalse(task.complete())


class DitauEffectiveEventSubmissionTests(unittest.TestCase):
    def test_refuses_to_submit_without_explicit_opt_in(self):
        task = DitauEffectiveEventSubmission(
            production="cp_2022_test", era="Run3_2022", tree="Events"
        )

        with self.assertRaisesRegex(Exception, "submission is disabled"):
            task.run()

    def test_records_successful_mock_submission_and_completes_intent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_dir = root / "submission-records"
            command = {
                "tree": "Events",
                "submits_jobs": True,
                "cwd": str(root),
                "argv": [
                    "/fake/python",
                    "/fake/run_analysis.py",
                    "--submission-manifest-dir",
                    str(manifest_dir),
                ],
            }
            plan = {"input_fingerprint": "plan-fingerprint", "commands": [command]}
            task = DitauEffectiveEventSubmission(
                production="test",
                era="Run3_2022",
                tree="Events",
                allow_submission=True,
                workspace=str(root),
            )

            def fake_run(arguments, cwd=None):
                manifest_dir.mkdir(parents=True, exist_ok=True)
                record = manifest_dir / "25_08_2026__Run3_2022__tt__Events.json"
                record.write_text('{"submitted": true}\n')
                return ""

            with patch.object(task, "plan", return_value=plan), patch.object(
                DitauEffectiveEventReadiness, "complete", return_value=True
            ), patch("workflow_automation.tasks.run_program", side_effect=fake_run):
                task.run()
                self.assertTrue(task.complete())

            receipt = json.loads(Path(task.output().path).read_text())
            self.assertEqual(receipt["tree"], "Events")
            intent = json.loads(task.intent_path().read_text())
            self.assertEqual(intent["status"], "completed")

    def test_records_submission_failure_in_intent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = {
                "tree": "Events",
                "submits_jobs": True,
                "cwd": str(root),
                "argv": [
                    "/fake/python",
                    "/fake/run_analysis.py",
                    "--submission-manifest-dir",
                    str(root / "submission-records"),
                ],
            }
            plan = {"input_fingerprint": "plan-fingerprint", "commands": [command]}
            task = DitauEffectiveEventSubmission(
                production="test",
                era="Run3_2022",
                tree="Events",
                allow_submission=True,
                workspace=str(root),
            )

            with patch.object(task, "plan", return_value=plan), patch.object(
                DitauEffectiveEventReadiness, "complete", return_value=True
            ), patch(
                "workflow_automation.tasks.run_program",
                side_effect=BootstrapError("diagnostic submission failure"),
            ):
                with self.assertRaisesRegex(BootstrapError, "diagnostic submission failure"):
                    task.run()

            intent = json.loads(task.intent_path().read_text())
            self.assertEqual(intent["status"], "failed")
            self.assertEqual(intent["error_type"], "BootstrapError")
            self.assertIn("diagnostic submission failure", intent["error"])
            self.assertIn("failed_at", intent)

    def test_captures_command_output_when_submission_records_nothing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = {
                "tree": "Events",
                "submits_jobs": True,
                "cwd": str(root),
                "argv": [
                    "/fake/python",
                    "/fake/run_analysis.py",
                    "--submission-manifest-dir",
                    str(root / "submission-records"),
                ],
            }
            plan = {"input_fingerprint": "plan-fingerprint", "commands": [command]}
            task = DitauEffectiveEventSubmission(
                production="test",
                era="Run3_2022",
                tree="Events",
                allow_submission=True,
                workspace=str(root),
            )

            # A command that exits successfully but submits nothing must leave a
            # durable transcript, otherwise the failure cannot be diagnosed later.
            with patch.object(task, "plan", return_value=plan), patch.object(
                DitauEffectiveEventReadiness, "complete", return_value=True
            ), patch(
                "workflow_automation.tasks.run_program",
                return_value="Requested to evaluate systematic variation without correction",
            ):
                with self.assertRaisesRegex(BootstrapError, "0 new or changed records"):
                    task.run()

            transcript = task.command_output_path()
            self.assertTrue(transcript.is_file())
            self.assertIn("systematic variation", transcript.read_text())
            intent = json.loads(task.intent_path().read_text())
            self.assertEqual(intent["status"], "failed")
            self.assertEqual(intent["command_output"], str(transcript))


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
