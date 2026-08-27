import json
import tempfile
import unittest
from pathlib import Path

from workflow_automation import provenance
from workflow_automation.cli import BootstrapError


class ProvenanceTests(unittest.TestCase):
    def inputs(self, root: Path) -> dict[str, Path]:
        samples = root / "samples.yaml"
        samples.write_text("a: 1\n")
        cross = root / "cross_sections.yaml"
        cross.write_text("b: 2\n")
        return {"samples": samples, "cross_sections": cross}

    def test_a_stamped_file_is_recognised_as_current(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs = self.inputs(root)
            artefact = root / "params.yaml"
            artefact.write_text("value: 1\n")
            payload = provenance.describe(inputs, {"era": "Run3_2022"})
            provenance.stamp(artefact, payload)

            self.assertTrue(provenance.matches(artefact, payload))
            self.assertEqual(provenance.read(artefact)["era"], "Run3_2022")

    def test_changing_an_input_makes_it_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs = self.inputs(root)
            artefact = root / "params.yaml"
            artefact.write_text("value: 1\n")
            provenance.stamp(artefact, provenance.describe(inputs))

            inputs["samples"].write_text("a: 2\n")
            self.assertFalse(provenance.matches(artefact, provenance.describe(inputs)))

    def test_the_header_is_a_comment_and_leaves_the_body_untouched(self):
        # Every downstream reader of these files must keep working unchanged, so
        # the record has to be something YAML ignores and the content must be
        # byte-identical to what the generating script wrote.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artefact = root / "stitching.yaml"
            body = "DY:\n  xs: 6077.22\n"
            artefact.write_text(body)
            provenance.stamp(artefact, provenance.describe(self.inputs(root)))

            lines = artefact.read_text().splitlines(keepends=True)
            self.assertTrue(lines[0].startswith("#"))
            self.assertEqual("".join(lines[1:]), body)

    def test_stamping_twice_does_not_accumulate_headers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs = self.inputs(root)
            artefact = root / "params.yaml"
            artefact.write_text("value: 1\n")
            provenance.stamp(artefact, provenance.describe(inputs))
            provenance.stamp(artefact, provenance.describe(inputs))

            headers = [
                line for line in artefact.read_text().splitlines()
                if line.startswith(provenance.MARKER)
            ]
            self.assertEqual(len(headers), 1)
            self.assertEqual(artefact.read_text().splitlines()[1], "value: 1")

    def test_a_file_we_did_not_write_is_protected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artefact = root / "params.yaml"
            artefact.write_text("hand written by somebody\n")
            with self.assertRaisesRegex(BootstrapError, "no provenance header"):
                provenance.guard_overwrite(artefact, allow_overwrite=False)
            provenance.guard_overwrite(artefact, allow_overwrite=True)

    def test_our_own_stale_file_is_not_protected(self):
        # Regenerating something this workflow made is the normal case and must
        # not need a flag; only somebody else's work does.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artefact = root / "params.yaml"
            artefact.write_text("value: 1\n")
            provenance.stamp(artefact, provenance.describe(self.inputs(root)))
            provenance.guard_overwrite(artefact, allow_overwrite=False)

    def test_a_missing_input_is_an_error_not_a_silent_pass(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(BootstrapError, "missing input"):
                provenance.describe({"absent": root / "nope.yaml"})

    def test_an_unstamped_file_never_matches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artefact = root / "params.yaml"
            artefact.write_text("value: 1\n")
            self.assertIsNone(provenance.read(artefact))
            self.assertFalse(provenance.matches(artefact, provenance.describe(self.inputs(root))))


if __name__ == "__main__":
    unittest.main()
