import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError
from workflow_automation.tasks import (
    DitauEffectiveEventSubmission,
    DitauStandardAnalysisReadiness,
    DitauStandardAnalysisSubmission,
)


class StandardAnalysisSubmissionTests(unittest.TestCase):
    """Step 6 carries the same safeguards as the effective-event submissions."""

    def command(self, root: Path, channel="tt", **extra):
        return {
            "stage": "standard-analysis",
            "era": "Run3_2022",
            "channel": channel,
            "submits_jobs": True,
            "cwd": str(root),
            "environment_bin": str(root / "envbin"),
            "argv": [
                "python", "run.py", "--channels", channel,
                "--submission-manifest-dir", str(root / "records"),
            ],
            **extra,
        }

    def task(self, root: Path, channel="tt", allow=False):
        return DitauStandardAnalysisSubmission(
            production="test", era="Run3_2022", channel=channel,
            allow_submission=allow, workspace=str(root),
        )

    def test_refuses_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(BootstrapError, "submission is disabled"):
                self.task(root).run()

    def test_an_unresolved_intent_blocks_retry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            task = self.task(root, allow=True)
            intent = task.intent_path()
            intent.parent.mkdir(parents=True, exist_ok=True)
            intent.write_text('{"status": "failed"}')
            with patch.object(DitauStandardAnalysisReadiness, "complete", return_value=True):
                with self.assertRaisesRegex(BootstrapError, "intent already exists"):
                    task.run()

    def test_each_channel_is_submitted_separately(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            commands = [self.command(root, c) for c in ("tt", "et", "mt")]
            task = self.task(root, channel="et")
            with patch.object(DitauStandardAnalysisReadiness, "commands", return_value=commands):
                self.assertEqual(task.command()["channel"], "et")

    def test_a_channel_the_plan_does_not_cover_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            commands = [self.command(root, "tt")]
            task = self.task(root, channel="mm")
            with patch.object(DitauStandardAnalysisReadiness, "commands", return_value=commands):
                with self.assertRaisesRegex(BootstrapError, "no unique standard-analysis"):
                    task.command()

    def test_records_are_matched_per_channel(self):
        # One record directory holds every channel, so a pattern that ignored the
        # channel would let one channel's record vouch for another's submission.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            records = root / "records"
            records.mkdir()
            for name in ("01_01_2026__Run3_2022__tt__Events.json",
                         "01_01_2026__Run3_2022__et__Events.json"):
                (records / name).write_text("{}")
            matched = sorted(p.name for p in records.glob("*__Run3_2022__et__*.json"))
            self.assertEqual(matched, ["01_01_2026__Run3_2022__et__Events.json"])

    def test_the_command_carries_the_analysis_environment(self):
        # Without this the workers inherit the controller interpreter, which is
        # how 1530 effective-event jobs once died on a missing numpy.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = self.command(root)
            environment = DitauEffectiveEventSubmission.command_environment(command)
            self.assertEqual(environment["PATH"].split(":")[0], str(root / "envbin"))


class StandardAnalysisReadinessTests(unittest.TestCase):
    def readiness(self, root: Path, channels=("tt", "et", "mt")):
        config = root / "productions.json"
        config.write_text(json.dumps({"productions": {"test": {
            "channels": list(channels), "analysis_type": "cp", "eras": ["Run3_2022"],
        }}}))
        return DitauStandardAnalysisReadiness(
            production="test", era="Run3_2022",
            productions_config=str(config), workspace=str(root),
        )

    def test_requires_stitching_and_params(self):
        # The standard analysis reads them, so it must not run before they exist.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            required = self.readiness(root).requires()
            self.assertEqual(sorted(required), ["params", "plan", "stitching"])

    def test_a_command_without_its_environment_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            task = self.readiness(root, channels=("tt",))
            command = {"stage": "standard-analysis", "era": "Run3_2022", "channel": "tt",
                       "submits_jobs": True, "cwd": str(root),
                       "argv": ["python", "run.py", "--help"]}
            with patch.object(type(task), "plan", return_value={"input_fingerprint": "x"}), \
                 patch.object(type(task), "commands", return_value=[command]), \
                 patch("workflow_automation.tasks.shutil.which", return_value="/usr/bin/condor_q"), \
                 patch("workflow_automation.tasks.GridCredentialCheck") as credential:
                credential.return_value.complete.return_value = True
                with self.assertRaisesRegex(BootstrapError, "missing its environment"):
                    task.run()


if __name__ == "__main__":
    unittest.main()
