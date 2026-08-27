import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_automation import provenance
from workflow_automation.cli import BootstrapError
from workflow_automation.tasks import (
    DitauEffectiveEventCounts,
    DitauParams,
    DitauStitching,
)


class DerivedArtefactTests(unittest.TestCase):
    """These files cost thousands of batch jobs, so 'do I need to rebuild' matters."""

    def build(self, root: Path) -> Path:
        checkout = root / "HiggsDNA"
        (checkout / "scripts/ditau/pre_processing").mkdir(parents=True)
        (checkout / "scripts/ditau/config/Run3_2022").mkdir(parents=True)
        (checkout / "scripts/ditau/pre_processing/samples_Run3_2022.yaml").write_text("DY: x\n")
        (checkout / "scripts/ditau/config/cross_sections.yaml").write_text("DY: 6077\n")
        (checkout / "scripts/ditau/config/Run3_2022/filter_efficiencies.yaml").write_text("a: 1\n")
        (checkout / "scripts/ditau/config/Run3_2022/effective_events.yaml").write_text("DY: 100\n")

        manifest = (
            root / ".workflow_automation/productions/test/sample-manifests/Run3_2022/samples"
        )
        manifest.mkdir(parents=True)
        (manifest / "samples_MC.json").write_text(json.dumps({"DY": ["a.root"]}))

        productions = root / "productions.json"
        productions.write_text(
            json.dumps(
                {"productions": {"test": {"effective_output": "output/effective/test"}}}
            )
        )
        return checkout

    def task(self, cls, root: Path, **kwargs):
        task = cls(
            production="test",
            era="Run3_2022",
            workspace=str(root),
            productions_config=str(root / "productions.json"),
            **kwargs,
        )
        patcher = patch.object(cls, "checkout", return_value=root / "HiggsDNA")
        patcher.start()
        self.addCleanup(patcher.stop)
        git = patch("workflow_automation.tasks.run_git", return_value="abc123")
        git.start()
        self.addCleanup(git.stop)
        return task

    def test_current_counts_mean_the_batch_jobs_are_not_needed(self):
        # The whole point: if what is already committed was derived from the
        # inputs still in use, nothing runs and neither tree is submitted.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = self.task(DitauEffectiveEventCounts, root)
            self.assertFalse(task.complete())
            provenance.stamp(task.artefact(), task.expected_provenance())
            self.assertTrue(task.complete())

    def test_a_changed_sample_manifest_makes_the_counts_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = self.task(DitauEffectiveEventCounts, root)
            provenance.stamp(task.artefact(), task.expected_provenance())
            self.assertTrue(task.complete())

            task.sample_manifest().write_text(json.dumps({"DY": ["a.root", "b.root"]}))
            self.assertFalse(task.complete())

    def test_the_analysis_configuration_does_not_invalidate_the_counts(self):
        # Counting generator weights does not depend on corrections or
        # systematics, so changing them must not cost thousands of jobs.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauEffectiveEventCounts, root)
            provenance.stamp(task.artefact(), task.expected_provenance())
            (checkout / "scripts/ditau/config/ditau_analysis.json").write_text('{"x": 1}')
            self.assertTrue(task.complete())

    def test_stitching_goes_stale_when_the_counts_change(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauStitching, root)
            (checkout / "scripts/ditau/config/Run3_2022/Stitching.yaml").write_text("DY: 1\n")
            provenance.stamp(task.artefact(), task.expected_provenance())
            self.assertTrue(task.complete())

            (checkout / "scripts/ditau/config/Run3_2022/effective_events.yaml").write_text("DY: 200\n")
            self.assertFalse(task.complete())

    def test_params_depends_on_filter_efficiencies(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauParams, root)
            (checkout / "scripts/ditau/config/Run3_2022/params.yaml").write_text("DY: 1\n")
            provenance.stamp(task.artefact(), task.expected_provenance())
            self.assertTrue(task.complete())

            (checkout / "scripts/ditau/config/Run3_2022/filter_efficiencies.yaml").write_text("a: 2\n")
            self.assertFalse(task.complete())

    def test_a_file_we_did_not_write_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = self.task(DitauEffectiveEventCounts, root)
            (root / "HiggsDNA/output/effective/test/Run3_2022").mkdir(parents=True)
            (root / "HiggsDNA/output/effective/test/Run3_2022/x.txt").write_text("1")
            with self.assertRaisesRegex(BootstrapError, "no provenance header"):
                task.run()

    def test_missing_batch_output_is_reported_rather_than_summed_to_nothing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = self.task(DitauEffectiveEventCounts, root, allow_overwrite=True)
            with self.assertRaisesRegex(BootstrapError, "no effective-event output"):
                task.run()


if __name__ == "__main__":
    unittest.main()
