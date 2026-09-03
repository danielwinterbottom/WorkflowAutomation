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
        processing = checkout / "scripts/ditau/processing"
        processing.mkdir(parents=True, exist_ok=True)
        for script in ("getEffectiveEvents.py", "getStitchingInfo.py", "getParams.py"):
            (processing / script).write_text("print('v1')\n")

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
            (checkout / "scripts/ditau/config/Run3_2022/params_DYfiltered.yaml").write_text("DY: 1\n")
            for item in [task.artefact(), *task.secondary_artefacts()]:
                provenance.stamp(item, task.expected_provenance())
            self.assertTrue(task.complete())

            (checkout / "scripts/ditau/config/Run3_2022/filter_efficiencies.yaml").write_text("a: 2\n")
            self.assertFalse(task.complete())

    def test_fixing_the_program_makes_its_output_stale(self):
        # A change to how the numbers are computed leaves every data input
        # identical, so without hashing the program the counts would look current
        # and quietly stay wrong.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauEffectiveEventCounts, root)
            provenance.stamp(task.artefact(), task.expected_provenance())
            self.assertTrue(task.complete())

            generator = checkout / "scripts/ditau/processing/getEffectiveEvents.py"
            generator.write_text("print('v2 - now subtracts negative weights')\n")
            self.assertFalse(task.complete())

    def test_each_artefact_watches_only_its_own_program(self):
        # Editing the params script must not invalidate the counts, which would
        # mean needing the batch output again for an unrelated change.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            counts = self.task(DitauEffectiveEventCounts, root)
            provenance.stamp(counts.artefact(), counts.expected_provenance())

            (checkout / "scripts/ditau/processing/getParams.py").write_text("print('v2')\n")
            self.assertTrue(counts.complete())

    def test_the_second_params_file_is_protected_too(self):
        # getParams writes params_DYfiltered.yaml as well. Not declaring it meant
        # it was overwritten without the guard ever being consulted.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauParams, root)
            (checkout / "scripts/ditau/config/Run3_2022/params.yaml").write_text("DY: 1\n")
            provenance.stamp(task.artefact(), task.expected_provenance())
            # somebody else's file, no header
            second = checkout / "scripts/ditau/config/Run3_2022/params_DYfiltered.yaml"
            second.write_text("hand written\n")
            with self.assertRaisesRegex(BootstrapError, "no provenance header"):
                task.run()

    def test_a_stale_second_file_makes_the_task_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = self.build(root)
            task = self.task(DitauParams, root)
            base = checkout / "scripts/ditau/config/Run3_2022"
            (base / "params.yaml").write_text("DY: 1\n")
            (base / "params_DYfiltered.yaml").write_text("DY: 1\n")
            for item in [task.artefact(), *task.secondary_artefacts()]:
                provenance.stamp(item, task.expected_provenance())
            self.assertTrue(task.complete())

            # the second file falls behind on its own
            (base / "params_DYfiltered.yaml").write_text("DY: 2\n")
            self.assertFalse(task.complete())

    def test_eras_without_a_filtered_file_declare_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.build(root)
            task = DitauParams(
                production="test", era="Run3_2024", workspace=str(root),
                productions_config=str(root / "productions.json"),
            )
            self.assertEqual(task.secondary_artefacts(), [])

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
