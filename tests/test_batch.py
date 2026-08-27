import json
import tempfile
import unittest
from pathlib import Path

from workflow_automation.batch import (
    APPLICATION,
    INFRASTRUCTURE,
    MEMORY,
    WALLTIME,
    classify_failure,
    load_site,
)
from workflow_automation.cli import BootstrapError


SITE = Path(__file__).resolve().parents[1] / "config" / "batch.json"


class ResourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site(SITE, "imperial")

    def test_three_hour_job_fits_one_slot(self):
        # short provides 8GB per slot, so 8GB inside three hours needs one core.
        resources = self.site.resolve(10799, 8000)
        self.assertEqual(resources.request_cpus, 1)

    def test_more_memory_within_three_hours_costs_cores(self):
        self.assertEqual(self.site.resolve(10799, 16000).request_cpus, 2)

    def test_longer_wall_time_costs_cores_to_keep_the_same_memory(self):
        # The point of the whole exercise: medium and long provide 4GB per slot,
        # so the same 8GB that ran on one core inside three hours needs two.
        short = self.site.resolve(10799, 8000)
        medium = self.site.resolve(35999, 8000)
        self.assertEqual(short.request_cpus, 1)
        self.assertEqual(medium.request_cpus, 2)
        self.assertEqual(medium.request_memory_mb, short.request_memory_mb)

    def test_refuses_runtime_no_node_class_can_host(self):
        with self.assertRaisesRegex(BootstrapError, "exceeds the longest node class"):
            self.site.resolve(200000, 4000)

    def test_refuses_a_request_needing_too_many_cores(self):
        with self.assertRaisesRegex(BootstrapError, "above the configured maximum"):
            self.site.resolve(172799, 999000)

    def test_submit_lines_use_the_site_runtime_attribute(self):
        lines = self.site.resolve(10799, 8000).submit_lines(self.site.runtime_attribute)
        self.assertIn("+MaxRuntime = 10799", lines)
        self.assertIn("request_cpus = 1", lines)
        self.assertIn("request_memory = 8000", lines)


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site(SITE, "imperial")

    def test_walltime_failure_buys_time_not_memory(self):
        first = self.site.next_step(WALLTIME, None)
        second = self.site.next_step(WALLTIME, first)
        self.assertGreater(second.runtime_seconds, first.runtime_seconds)

    def test_memory_failure_buys_memory_not_time(self):
        first = self.site.next_step(MEMORY, None)
        second = self.site.next_step(MEMORY, first)
        self.assertGreater(second.request_memory_mb, first.request_memory_mb)
        self.assertEqual(second.runtime_seconds, first.runtime_seconds)

    def test_application_errors_are_never_resubmitted(self):
        with self.assertRaisesRegex(BootstrapError, "not resubmittable"):
            self.site.next_step(APPLICATION, None)

    def test_the_ladder_ends_rather_than_looping(self):
        current = self.site.next_step(WALLTIME, None)
        for _ in range(len(self.site.runtime_ladder) + 1):
            try:
                current = self.site.next_step(WALLTIME, current)
            except BootstrapError as error:
                self.assertIn("needs splitting", str(error))
                return
        self.fail("escalation never terminated")

    def test_escalating_time_does_not_silently_shrink_memory(self):
        # The trap this whole model exists to avoid: a longer retry moves the job
        # onto 4GB slots, so its memory must be preserved by adding cores.
        first = self.site.next_step(WALLTIME, None)
        second = self.site.next_step(WALLTIME, first)
        self.assertEqual(second.request_memory_mb, first.request_memory_mb)
        self.assertGreater(second.request_cpus, first.request_cpus)

    def test_infrastructure_failures_retry_unchanged(self):
        first = self.site.next_step(WALLTIME, None)
        again = self.site.next_step(INFRASTRUCTURE, first)
        self.assertEqual(again, first)

    def test_cores_stay_at_one_where_a_single_slot_suffices(self):
        for memory in (4000, 8000, 12000):
            self.assertEqual(self.site.resolve(10799, memory).request_cpus, 1, memory)


class FailureClassificationTests(unittest.TestCase):
    def test_condor_memory_kill(self):
        self.assertEqual(classify_failure(log_text="Job was held: memory usage exceeded"), MEMORY)

    def test_condor_walltime_kill(self):
        self.assertEqual(
            classify_failure(log_text="removed: job exceeded MaxRuntime"), WALLTIME
        )

    def test_application_error(self):
        stderr = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'numpy'"
        self.assertEqual(classify_failure(stderr_text=stderr), APPLICATION)

    def test_a_memory_kill_mid_traceback_is_not_a_code_fault(self):
        # An out-of-memory kill often lands in the middle of Python unwinding.
        # Reading that as an application error would stop us retrying a job that
        # a larger slot would finish.
        self.assertEqual(
            classify_failure(
                log_text="Job was held: memory usage exceeded",
                stderr_text="Traceback (most recent call last):\nMemoryError",
            ),
            MEMORY,
        )

    def test_eviction_is_infrastructure(self):
        self.assertEqual(classify_failure(log_text="Job was evicted."), INFRASTRUCTURE)

    def test_the_numpy_failure_from_this_project_is_not_retried(self):
        # The real 1530-job failure. Retrying it would have wasted the farm.
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "run_analysis.py", line 9, in <module>\n'
            "    import numpy as np\n"
            "ModuleNotFoundError: No module named 'numpy'\n"
        )
        cause = classify_failure(log_text="Normal termination (return value 1)", stderr_text=stderr)
        self.assertEqual(cause, APPLICATION)


class ConfigurationTests(unittest.TestCase):
    def test_unknown_site_is_rejected(self):
        with self.assertRaisesRegex(BootstrapError, "unknown batch site"):
            load_site(SITE, "nowhere")

    def test_missing_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(BootstrapError, "not found"):
                load_site(Path(temporary_directory) / "absent.json", "imperial")

    def test_site_without_node_classes_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "batch.json"
            path.write_text(json.dumps({"sites": {"empty": {"node_classes": []}}}))
            with self.assertRaisesRegex(BootstrapError, "no node classes"):
                load_site(path, "empty")


if __name__ == "__main__":
    unittest.main()
