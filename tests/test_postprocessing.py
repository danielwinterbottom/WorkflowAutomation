import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation.cli import BootstrapError
from workflow_automation.tasks import (
    DitauMergeParquet,
    DitauParquetToRoot,
    DitauStandardAnalysisStatus,
)


class PostProcessingTests(unittest.TestCase):
    """The steps between the analysis and anything that reads ROOT files."""

    def setup(self, root: Path):
        config = root / "productions.json"
        config.write_text(json.dumps({"productions": {"test": {
            "analysis_type": "cp", "channels": ["mm"], "eras": ["Run3_2022"],
            "output": "output/test",
        }}}))
        return config

    def merge(self, root: Path, allow=True):
        return DitauMergeParquet(
            production="test", era="Run3_2022", channel="mm", allow_submission=allow,
            productions_config=str(self.setup(root)), workspace=str(root),
        )

    def convert(self, root: Path, allow=True):
        return DitauParquetToRoot(
            production="test", era="Run3_2022", channel="mm", allow_submission=allow,
            productions_config=str(self.setup(root)), workspace=str(root),
        )

    def test_each_step_has_its_own_record_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertEqual(
                self.merge(root).record_pattern(), "*__Run3_2022__mm__merge.json"
            )
            self.assertEqual(
                self.convert(root).record_pattern(), "*__Run3_2022__mm__parquetToRoot.json"
            )

    def test_the_two_steps_do_not_share_state(self):
        # A shared directory would let one step's receipt satisfy the other.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertNotEqual(self.merge(root).output().path, self.convert(root).output().path)
            self.assertNotEqual(self.merge(root).records_dir(), self.convert(root).records_dir())

    def test_neither_step_runs_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for task in (self.merge(root, allow=False), self.convert(root, allow=False)):
                with self.assertRaisesRegex(BootstrapError, "submission is disabled"):
                    task.run()

    def test_merging_refuses_a_partly_finished_analysis(self):
        # Merging partial output produces something that looks complete and is not.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            task = self.merge(root)
            classified = {"totals": {"expected": 100, "completed": 97, "failed": 3, "pending": 0}}
            with patch.object(DitauStandardAnalysisStatus, "jobs_directory", return_value=root), \
                 patch.object(DitauStandardAnalysisStatus, "classify", return_value=classified):
                with self.assertRaisesRegex(BootstrapError, "not complete"):
                    task.run()

    def test_conversion_refuses_when_datasets_are_unmerged(self):
        # The merge receipt proves submission, not completion, so the merged
        # files themselves decide.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            task = self.convert(root)
            produced = root / "HiggsDNA/output/test/Run3_2022/mm"
            (produced / "DY" / "nominal").mkdir(parents=True)
            (produced / "DY" / "nominal" / "merged.parquet").write_text("x")
            (produced / "TT" / "nominal").mkdir(parents=True)  # no merged.parquet
            with patch.object(DitauMergeParquet, "complete", return_value=True), \
                 patch.object(DitauParquetToRoot, "checkout", return_value=root / "HiggsDNA"):
                with self.assertRaisesRegex(BootstrapError, "no merged.parquet"):
                    task.run()

    def test_conversion_refuses_without_a_merge_receipt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            task = self.convert(root)
            with patch.object(DitauMergeParquet, "complete", return_value=False):
                with self.assertRaisesRegex(BootstrapError, "no valid receipt"):
                    task.run()

    def test_a_status_probe_is_not_declared_as_a_requirement(self):
        """Luigi refuses to run a task whose dependency is unfulfilled.

        The status tasks never report themselves complete, by design, so
        declaring one as a requirement makes the depending task unrunnable at
        exactly the moment it is needed. They are consulted directly instead.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for task in (self.merge(root), self.convert(root)):
                required = task.requires()
                flat = required if isinstance(required, (list, tuple)) else [required]
                flat = [item for item in flat if item is not None]
                self.assertFalse(
                    any(isinstance(item, DitauStandardAnalysisStatus) for item in flat),
                    f"{type(task).__name__} must not require a status task",
                )

    def test_the_command_names_the_channel_and_the_record_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            argv = self.merge(root).command()
            self.assertIn("merge_parquet.py", argv[1])
            self.assertIn("--use_condor", argv)
            self.assertEqual(argv[argv.index("--channels") + 1], "mm")
            self.assertIn("merge-records", argv[argv.index("--submission-manifest-dir") + 1])


if __name__ == "__main__":
    unittest.main()


class WorkflowOutputTests(unittest.TestCase):
    """Products are named for the workflow that made them, not where they landed."""

    def test_workflows_over_one_production_sit_side_by_side(self):
        from workflow_automation.tasks import workflow_output
        plots = workflow_output("cp_2022_test", "control-plots")
        cards = workflow_output("cp_2022_test", "datacards")
        self.assertEqual(plots.parent, cards.parent)
        self.assertEqual(plots.parent.name, "cp_2022_test")
        self.assertEqual(plots.name, "control-plots")

    def test_productions_do_not_share_a_directory(self):
        from workflow_automation.tasks import workflow_output
        self.assertNotEqual(
            workflow_output("cp_2022_test", "control-plots"),
            workflow_output("cp_2023_test", "control-plots"),
        )

    def test_a_production_may_place_its_output_elsewhere(self):
        # Products can be large; a production should be able to put them on
        # whichever filesystem has room without moving the workflow.
        from workflow_automation.tasks import workflow_output
        self.assertTrue(
            str(workflow_output("test", "control-plots", "/vols/elsewhere")).startswith(
                "/vols/elsewhere"
            )
        )

    def test_the_plan_uses_the_named_workflow_directory(self):
        from workflow_automation.tasks import TidalControlPlotPlan
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "productions.json"
            config.write_text(json.dumps({"productions": {"test": {
                "analysis_type": "cp", "output": "output/test", "eras": ["Run3_2022"],
            }}}))
            plan = TidalControlPlotPlan(
                production="test", era="Run3_2022", channel="mm",
                productions_config=str(config), workspace=str(root),
            )
            out = plan.plot_output()
            # era, scheme and channel are appended by makeDatacards itself
            self.assertEqual(out.parts[-2:], ("test", "control-plots"))


class PlotPlanFingerprintTests(unittest.TestCase):
    def plan(self, root: Path, output_root=None):
        from workflow_automation.tasks import TidalControlPlotPlan
        config = root / "productions.json"
        production = {"analysis_type": "cp", "output": "output/test", "eras": ["Run3_2022"]}
        if output_root:
            production["output_root"] = output_root
        config.write_text(json.dumps({"productions": {"test": production}}))
        return TidalControlPlotPlan(
            production="test", era="Run3_2022", channel="mm",
            productions_config=str(config), workspace=str(root),
        )

    def test_moving_the_output_rebuilds_the_configuration(self):
        # Otherwise the configuration keeps naming somewhere the products are not.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = root / "TIDAL/Draw/scripts/config_plot_LateRun3.yaml"
            template.parent.mkdir(parents=True)
            template.write_text("control: {}\n")
            with patch.object(type(self.plan(root)), "template_path", return_value=template):
                here = self.plan(root).current_fingerprint()
                elsewhere = self.plan(root, "/vols/elsewhere").current_fingerprint()
            self.assertNotEqual(here, elsewhere)
