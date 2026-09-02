import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError, Repository
from workflow_automation.tasks import (
    DitauInputPreparation,
    DitauEffectiveEventPlan,
    DitauEffectiveEventReadiness,
    DitauEffectiveEventResubmission,
    DitauEffectiveEventStatus,
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

            def fake_run(arguments, cwd=None, env=None):
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

    def test_command_environment_puts_analysis_interpreter_first(self):
        # Condor submit files use `getenv = True` and the generated job wrappers
        # call a bare `python3`, so whatever is first on PATH at submission time
        # is what the workers run.
        command = {"environment_bin": "/envs/HiggsDNA/bin"}
        with patch.dict(
            "os.environ", {"PATH": "/controller/.venv/bin:/usr/bin"}, clear=False
        ):
            environment = DitauEffectiveEventSubmission.command_environment(command)
        self.assertIsNotNone(environment)
        self.assertEqual(
            environment["PATH"].split(":")[0], "/envs/HiggsDNA/bin"
        )

    def test_command_environment_is_inherited_when_unspecified(self):
        self.assertIsNone(DitauEffectiveEventSubmission.command_environment({}))

    def test_plan_schema_version_changes_the_fingerprint(self):
        # A plan's inputs can be unchanged while the code that builds its commands
        # changes. Without the schema version in the fingerprint, the stale plan
        # keeps looking current and the new commands are never generated.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            productions = root / "productions.json"
            productions.write_text(
                json.dumps(
                    {
                        "productions": {
                            "test": {
                                "eras": ["Run3_2022"],
                                "effective_output": "output/effective/test",
                            }
                        }
                    }
                )
            )
            task = DitauEffectiveEventPlan(
                production="test",
                era="Run3_2022",
                productions_config=str(productions),
                workspace=str(root),
            )
            receipt = root / "receipt.json"
            receipt.write_text("{}")
            analysis = root / "ditau_analysis.json"
            analysis.write_text("{}")

            with patch.object(task, "checkout", return_value=(None, root)), patch.object(
                task, "sample_receipt", return_value=receipt
            ), patch(
                "workflow_automation.tasks.run_git", return_value="deadbeef"
            ), patch(
                "pathlib.Path.read_bytes", autospec=True, side_effect=lambda self: b"x"
            ):
                original = task.current_fingerprint()
                with patch.object(DitauEffectiveEventPlan, "SCHEMA_VERSION", 99):
                    bumped = task.current_fingerprint()

            self.assertNotEqual(original, bumped)


class DitauEffectiveEventStatusTests(unittest.TestCase):
    """A status check that can only ever report success is not a check."""

    MARKER = "Processing 100% \u2501\u2501 3/3"

    def build_jobs_dir(self, root: Path) -> Path:
        jobs = root / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "AN-Sample.sub").write_text("executable = x\nqueue 3\n")
        # proc 0 finished, proc 1 ran but never reached the end, proc 2 never ran
        (jobs / "AN-Sample.100.0.out").write_text(f"working\n{self.MARKER}\n")
        (jobs / "AN-Sample.100.1.out").write_text("started and then died\n")
        return jobs

    def status_task(self, root: Path) -> DitauEffectiveEventStatus:
        return DitauEffectiveEventStatus(
            production="test", era="Run3_2022", tree="Events", workspace=str(root)
        )

    def test_distinguishes_completed_failed_and_pending(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build_jobs_dir(root)
            result = self.status_task(root).classify(jobs)

            self.assertEqual(
                result["totals"],
                {"expected": 3, "completed": 1, "failed": 1, "pending": 1},
            )
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(result["failures"][0]["proc"], 1)

    def test_records_why_each_job_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build_jobs_dir(root)
            # proc 1 already exists as a failure with no explanation; give it the
            # real thing this farm writes for a wall clock kill.
            (jobs / "AN-Sample.100.log").write_text(
                "000 (100.001.000) Job submitted\n...\n"
                "012 (100.001.000) Job was held.\n"
                "\tJob held by SYSTEM_PERIODIC_HOLD due to wall time exceeded.\n...\n"
            )
            result = self.status_task(root).classify(jobs)

            self.assertEqual(result["causes"], {"walltime": 1})
            failure = result["failures"][0]
            self.assertEqual(failure["cause"], "walltime")
            self.assertTrue(failure["retryable"])

    def test_an_application_failure_is_marked_not_retryable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build_jobs_dir(root)
            (jobs / "AN-Sample.100.1.err").write_text(
                "Traceback (most recent call last):\nModuleNotFoundError: No module named 'numpy'\n"
            )
            result = self.status_task(root).classify(jobs)

            self.assertEqual(result["causes"], {"application": 1})
            self.assertFalse(result["failures"][0]["retryable"])

    def test_one_proc_hold_is_not_attributed_to_its_siblings(self):
        # Condor writes one log per cluster, so reading the whole file would make
        # every proc in it look held.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build_jobs_dir(root)
            (jobs / "AN-Sample.100.2.out").write_text("started, no marker\n")
            (jobs / "AN-Sample.100.log").write_text(
                "012 (100.002.000) Job was held.\n"
                "\tJob held by SYSTEM_PERIODIC_HOLD due to wall time exceeded.\n...\n"
            )
            result = self.status_task(root).classify(jobs)

            by_proc = {item["proc"]: item["cause"] for item in result["failures"]}
            self.assertEqual(by_proc[2], "walltime")
            self.assertNotEqual(by_proc[1], "walltime")

    def test_uses_the_most_recent_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build_jobs_dir(root)
            # A resubmission writes a new cluster id for the same proc. The later
            # attempt succeeded, so the job must no longer count as failed.
            retry = jobs / "AN-Sample.200.1.out"
            retry.write_text(f"retried\n{self.MARKER}\n")
            os.utime(retry, (10**9, 10**9))
            for stale in jobs.glob("AN-Sample.100.*.out"):
                os.utime(stale, (10**8, 10**8))

            result = self.status_task(root).classify(jobs)
            self.assertEqual(result["totals"]["failed"], 0)
            self.assertEqual(result["totals"]["completed"], 2)

    def test_refuses_to_report_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(BootstrapError, "no submission receipt"):
                self.status_task(root).jobs_directory()

    def test_never_reports_itself_complete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertFalse(self.status_task(root).complete())

    def test_queue_count_ignores_unrelated_jobs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = root / "jobs"
            # The operator runs other work under the same account on the same schedd.
            listing = f"{jobs}/AN-Sample.sh\n/vols/cms/other/TauPolaris/train.sh\n"
            with patch("workflow_automation.tasks.shutil.which", return_value="/usr/bin/condor_q"), \
                 patch("workflow_automation.tasks.run_program", return_value=listing):
                self.assertEqual(DitauEffectiveEventStatus.queued_job_count(jobs), 1)

    def test_queue_count_is_unknown_when_condor_is_absent(self):
        with patch("workflow_automation.tasks.shutil.which", return_value=None):
            self.assertIsNone(DitauEffectiveEventStatus.queued_job_count(Path("/x")))


class DitauEffectiveEventResubmissionTests(unittest.TestCase):
    def task(self, root: Path, allow=False) -> DitauEffectiveEventResubmission:
        return DitauEffectiveEventResubmission(
            production="test",
            era="Run3_2022",
            tree="Events",
            allow_submission=allow,
            workspace=str(root),
        )

    MARKER = "Processing 100% \u2501\u2501 3/3"

    def write_jobs(self, root: Path, failed=0, pending=0, total=3) -> Path:
        """Build a real job directory, plus the receipt and record pointing at it."""
        jobs = root / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / "AN-Sample.sub").write_text(f"executable = x\nqueue {total}\n")
        for index in range(total):
            if index < total - failed - pending:
                (jobs / f"AN-Sample.1.{index}.out").write_text(f"ok\n{self.MARKER}\n")
            elif index < total - pending:
                (jobs / f"AN-Sample.1.{index}.out").write_text("died early\n")
            # anything left has no .out at all, so it counts as pending

        state = self.task(root).state_dir()
        record = state / "submission-records" / "rec.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"jobs_dir": str(jobs)}))
        receipt = state / "submission-receipts" / "Events.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"submission_record": str(record)}))
        return jobs

    def test_complete_means_no_jobs_left_to_fix(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, failed=0, pending=0)
            self.assertTrue(self.task(root).complete())

    def test_incomplete_while_jobs_are_still_outstanding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            # Having resubmitted once must not count as complete: what matters is
            # whether the jobs are done, not whether a command was run.
            self.write_jobs(root, failed=2)
            task = self.task(root)
            receipt = Path(task.output().path)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("{}")
            self.assertFalse(task.complete())

    def test_pending_jobs_also_count_as_outstanding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, pending=2)
            self.assertFalse(self.task(root).complete())

    def test_refuses_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, failed=1)
            with self.assertRaisesRegex(BootstrapError, "resubmission is disabled"):
                self.task(root).run()

    def test_refuses_when_nothing_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, failed=0, pending=0)
            with self.assertRaisesRegex(BootstrapError, "nothing to resubmit"):
                self.task(root, allow=True).run()

    def test_a_stale_status_report_cannot_declare_completeness(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, failed=1)
            # A report written when everything was fine must not outvote the jobs.
            task = self.task(root)
            report = Path(task.status().output().path)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "jobs_dir": str(root / "jobs"),
                        "totals": {
                            "expected": 3, "completed": 3, "failed": 0, "pending": 0
                        },
                    }
                )
            )
            self.assertFalse(task.complete())

    def test_an_unresolved_intent_blocks_retry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_jobs(root, failed=1)
            task = self.task(root, allow=True)
            intent = task.intent_path()
            intent.parent.mkdir(parents=True, exist_ok=True)
            intent.write_text('{"status": "failed"}')
            with self.assertRaisesRegex(BootstrapError, "intent already exists"):
                task.run()


class SkipCompletedDatasetsTests(unittest.TestCase):
    """Widening a production should cost the jobs it adds, not the ones already done."""

    MARKER = "Processing 100% \u2501\u2501 3/3"

    def build(self, root: Path, finished=("DY", "TT"), pending=("BBH_M_1000",)):
        jobs = root / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        for name in finished:
            (jobs / f"AN-{name}.sub").write_text("executable = x\nqueue 1\n")
            (jobs / f"AN-{name}.100.0.out").write_text(f"ok\n{self.MARKER}\n")
        # a dataset that was submitted but never finished must not be skipped
        (jobs / "AN-HALF.sub").write_text("executable = x\nqueue 2\n")
        (jobs / "AN-HALF.100.0.out").write_text(f"ok\n{self.MARKER}\n")
        (jobs / "AN-HALF.100.1.out").write_text("died\n")

        state = root / ".workflow_automation/productions/test/effective-events/Run3_2022"
        (state / "submission-records").mkdir(parents=True, exist_ok=True)
        (state / "submission-records/rec.json").write_text(json.dumps({"jobs_dir": str(jobs)}))
        (state / "submission-receipts").mkdir(parents=True, exist_ok=True)
        (state / "submission-receipts/Events.json").write_text(
            json.dumps({"submission_record": str(state / "submission-records/rec.json")})
        )

        manifest = root / "samples_MC.json"
        everything = list(finished) + ["HALF"] + list(pending)
        manifest.write_text(json.dumps({name: [f"{name}.root"] for name in everything}))
        analysis = root / "Events.json"
        analysis.write_text(json.dumps({"samplejson": str(manifest), "year": "Run3_2022"}))
        return {"tree": "Events", "cwd": str(root),
                "argv": ["python", "run_analysis.py", "--json-analysis", str(analysis)]}

    def task(self, root: Path):
        return DitauEffectiveEventSubmission(
            production="test", era="Run3_2022", tree="Events",
            allow_submission=True, skip_completed=True, workspace=str(root),
        )

    def test_finished_datasets_are_left_out(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = self.build(root)
            narrowed, skipped = self.task(root).narrow_to_outstanding(command)

            self.assertEqual(skipped, ["DY", "TT"])
            reduced = json.loads(Path(json.loads(
                Path(narrowed["argv"][3]).read_text())["samplejson"]).read_text())
            self.assertEqual(sorted(reduced), ["BBH_M_1000", "HALF"])

    def test_a_partly_finished_dataset_is_still_submitted(self):
        # Skipping it would strand the jobs that never completed.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = self.build(root)
            _, skipped = self.task(root).narrow_to_outstanding(command)
            self.assertNotIn("HALF", skipped)

    def test_the_plan_files_are_not_modified(self):
        # The plan must keep describing the whole production.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = self.build(root)
            before = (root / "samples_MC.json").read_text()
            self.task(root).narrow_to_outstanding(command)
            self.assertEqual((root / "samples_MC.json").read_text(), before)

    def test_nothing_outstanding_is_refused_rather_than_submitted_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = self.build(root, finished=("DY",), pending=())
            (root / "jobs/AN-HALF.100.1.out").write_text(f"ok\n{self.MARKER}\n")
            with self.assertRaisesRegex(BootstrapError, "nothing to submit"):
                self.task(root).narrow_to_outstanding(command)

    def test_a_fresh_production_skips_nothing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = root / "samples_MC.json"
            manifest.write_text(json.dumps({"DY": ["a.root"]}))
            analysis = root / "Events.json"
            analysis.write_text(json.dumps({"samplejson": str(manifest)}))
            command = {"tree": "Events", "cwd": str(root),
                       "argv": ["python", "run_analysis.py", "--json-analysis", str(analysis)]}
            narrowed, skipped = self.task(root).narrow_to_outstanding(command)
            self.assertEqual(skipped, [])
            self.assertEqual(narrowed, command)


class ResubmissionGenerationTests(unittest.TestCase):
    """The generated submit files are what the farm actually acts on."""

    MARKER = "Processing 100% \u2501\u2501 3/3"
    HOLD = (
        "012 (100.001.000) Job was held.\n"
        "\tJob held by SYSTEM_PERIODIC_HOLD due to wall time exceeded.\n...\n"
    )

    def build(self, root: Path, stderr: str = "", log: str = "") -> Path:
        jobs = root / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        # The original submission: three hours, 8GB, one core.
        (jobs / "AN-Sample.sub").write_text(
            "executable = x\nrequest_cpus = 1\nrequest_memory = 8000\n"
            "+MaxRuntime = 10799\nqueue 2\n"
        )
        (jobs / "AN-Sample.sh").write_text("#!/bin/sh\necho hi\n")
        (jobs / "AN-Sample.100.0.out").write_text(f"ok\n{self.MARKER}\n")
        (jobs / "AN-Sample.100.1.out").write_text("died\n")
        if stderr:
            (jobs / "AN-Sample.100.1.err").write_text(stderr)
        (jobs / "AN-Sample.100.log").write_text(log or self.HOLD)

        task = self.task(root)
        state = task.state_dir()
        record = state / "submission-records" / "rec.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"jobs_dir": str(jobs)}))
        receipt = state / "submission-receipts" / "Events.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"submission_record": str(record)}))
        (state / "plan.json").write_text(
            json.dumps(
                {
                    "commands": [
                        {"tree": "Events", "cwd": str(root),
                         "environment_bin": str(root / "envbin")}
                    ]
                }
            )
        )
        return jobs

    def task(self, root: Path, allow=True):
        return DitauEffectiveEventResubmission(
            production="test", era="Run3_2022", tree="Events",
            allow_submission=allow, workspace=str(root),
        )

    def run_task(self, root: Path):
        calls = []
        task = self.task(root)
        with patch(
            "workflow_automation.tasks.run_program",
            side_effect=lambda argv, cwd=None, env=None: calls.append(argv) or "",
        ):
            task.run()
        return task, calls

    def test_a_timeout_is_resubmitted_as_a_single_core_medium_job(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build(root)
            task, calls = self.run_task(root)

            submitted = jobs / "workflow_resubmit" / "AN-Sample.1.sub"
            self.assertTrue(submitted.is_file())
            body = submitted.read_text()
            self.assertIn("request_cpus = 1", body)
            self.assertIn("request_memory = 4000", body)
            self.assertIn("+MaxRuntime = 35999", body)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "condor_submit")

    def test_the_submit_file_keeps_the_original_output_naming(self):
        # The status task finds the newest attempt by globbing these names, so a
        # retry writing elsewhere would look like it never ran.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build(root)
            self.run_task(root)
            body = (jobs / "workflow_resubmit" / "AN-Sample.1.sub").read_text()
            self.assertIn(str(jobs / "AN-Sample.$(ClusterId).1.out"), body)
            self.assertIn("arguments = 1", body)

    def test_the_workers_still_inherit_the_submitting_environment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = self.build(root)
            self.run_task(root)
            self.assertIn(
                "getenv = True", (jobs / "workflow_resubmit" / "AN-Sample.1.sub").read_text()
            )

    def test_an_application_failure_is_never_resubmitted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(
                root,
                stderr="Traceback (most recent call last):\nModuleNotFoundError: no numpy\n",
                log="000 (100.001.000) Job submitted\n...\n",
            )
            with self.assertRaisesRegex(BootstrapError, "nothing can be resubmitted"):
                self.task(root).run()

    def test_demonstrated_needs_survive_into_the_next_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task, _ = self.run_task(root)

            recorded = json.loads(task.state_path().read_text())["jobs"]["AN-Sample:1"]
            self.assertEqual(recorded["attempts"], 1)
            # It overran three hours, so it must never be offered three hours again.
            self.assertEqual(recorded["minimum_runtime_seconds"], 10800)
            # It said nothing about memory, so no floor was invented for it.
            self.assertEqual(recorded["minimum_memory_mb"], 0)

    def test_attempts_are_capped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = self.task(root)
            task.save_state(
                {"AN-Sample:1": {"attempts": 3, "minimum_runtime_seconds": 0,
                                 "minimum_memory_mb": 0}}
            )
            with self.assertRaisesRegex(BootstrapError, "nothing can be resubmitted"):
                task.run()


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
