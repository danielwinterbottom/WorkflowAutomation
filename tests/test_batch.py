import json
import tempfile
import unittest
from pathlib import Path

from workflow_automation.batch import (
    APPLICATION,
    UNKNOWN,
    Demand,
    INFRASTRUCTURE,
    MEMORY,
    WALLTIME,
    classify_failure,
    load_site,
)
from workflow_automation.cli import BootstrapError


SITE = Path(__file__).resolve().parents[1] / "config" / "batch.json"


class SlotSelectionTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site(SITE, "imperial")

    def named(self, slot):
        return (slot.node_class.name, slot.cpus)

    def test_a_job_with_no_demonstrated_needs_gets_the_cheapest_slot(self):
        self.assertEqual(self.named(self.site.select(Demand())), ("short", 1))

    def test_extra_memory_is_bought_from_a_bigger_slot_before_a_second_core(self):
        chosen = self.site.select(Demand(minimum_memory_mb=9000))
        self.assertEqual(self.named(chosen), ("short-highmem", 1))

    def test_cores_are_only_spent_when_no_single_slot_suffices(self):
        self.assertEqual(self.named(self.site.select(Demand(minimum_memory_mb=13000))), ("short", 2))

    def test_refuses_what_no_slot_can_provide(self):
        with self.assertRaisesRegex(BootstrapError, "split differently"):
            self.site.select(Demand(minimum_runtime_seconds=200000))


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site(SITE, "imperial")

    def named(self, slot):
        return (slot.node_class.name, slot.cpus)

    def walk(self, causes):
        slot, demand = self.site.next_step(WALLTIME, None)
        steps = [self.named(slot)]
        for cause in causes:
            slot, demand = self.site.next_step(cause, slot, demand)
            steps.append(self.named(slot))
        return steps

    def test_a_timeout_moves_to_a_single_core_medium_job(self):
        # The whole point: overrunning says nothing about memory, so the retry
        # must not spend a second core carrying memory the job never needed.
        self.assertEqual(self.walk([WALLTIME]), [("short", 1), ("medium", 1)])

    def test_repeated_timeouts_try_medium_before_long(self):
        self.assertEqual(
            self.walk([WALLTIME, WALLTIME]), [("short", 1), ("medium", 1), ("long", 1)]
        )

    def test_unobserved_memory_is_not_carried_across_a_time_escalation(self):
        # The first slot happens to provide 8GB. That is what short gives, not
        # something the job asked for, so it must not become a floor.
        slot, demand = self.site.next_step(WALLTIME, None)
        self.assertEqual(slot.memory_mb, 8000)
        escalated, updated = self.site.next_step(WALLTIME, slot, demand)
        self.assertEqual(updated.minimum_memory_mb, 0)
        self.assertEqual(escalated.memory_mb, 4000)

    def test_observed_memory_is_carried_across_a_time_escalation(self):
        # Once a job has actually run out of memory, that floor must survive a
        # later escalation for time, even though it costs a core.
        steps = self.walk([WALLTIME, MEMORY, WALLTIME])
        self.assertEqual(steps, [("short", 1), ("medium", 1), ("medium", 2), ("long", 2)])

    def test_memory_escalation_leaves_the_wall_clock_alone(self):
        slot, demand = self.site.next_step(WALLTIME, None)
        bigger, _ = self.site.next_step(MEMORY, slot, demand)
        self.assertEqual(bigger.runtime_seconds, slot.runtime_seconds)
        self.assertGreater(bigger.memory_mb, slot.memory_mb)

    def test_infrastructure_failures_retry_unchanged(self):
        slot, demand = self.site.next_step(WALLTIME, None)
        again, _ = self.site.next_step(INFRASTRUCTURE, slot, demand)
        self.assertEqual(again, slot)

    def test_application_errors_are_never_resubmitted(self):
        with self.assertRaisesRegex(BootstrapError, "not resubmittable"):
            self.site.next_step(APPLICATION, None)

    def test_escalation_terminates(self):
        slot, demand = self.site.next_step(WALLTIME, None)
        for _ in range(len(self.site.node_classes) + 2):
            try:
                slot, demand = self.site.next_step(WALLTIME, slot, demand)
            except BootstrapError as error:
                self.assertIn("split differently", str(error))
                return
        self.fail("escalation never terminated")

    def test_never_exceeds_the_configured_core_cap(self):
        for slot in self.site.slots():
            self.assertLessEqual(slot.cpus, self.site.max_cpus_per_job)


class FailureClassificationTests(unittest.TestCase):
    def test_condor_memory_kill(self):
        self.assertEqual(classify_failure(log_text="Job was held: memory usage exceeded"), MEMORY)

    def test_the_real_imperial_walltime_hold(self):
        """The exact text this farm writes, captured from a deliberately overrun job.

        Guessed patterns missed it: Imperial holds the job through
        SYSTEM_PERIODIC_HOLD and says "wall time exceeded", so neither
        "MaxRuntime" nor "maximum" appears anywhere. The classifier previously
        fell through to `incomplete` and would have retried the job with the
        same wall clock until the attempt cap stopped it.
        """
        log = (Path(__file__).parent / "fixtures" / "condor-walltime-hold.log").read_text()
        self.assertEqual(classify_failure(log_text=log), WALLTIME)

    def test_a_hold_we_cannot_read_is_not_blindly_retried(self):
        # Holds are how this farm enforces limits. An unrecognised one is a limit
        # we have not learned to read, so retrying unchanged would just hit it again.
        log = "012 (1.0.0) Job was held.\n\tJob held by SYSTEM_PERIODIC_HOLD due to something new.\n"
        self.assertEqual(classify_failure(log_text=log), UNKNOWN)

    def test_a_memory_hold_is_still_read_as_memory(self):
        log = "012 (1.0.0) Job was held.\n\tJob held by SYSTEM_PERIODIC_HOLD due to memory usage exceeded.\n"
        self.assertEqual(classify_failure(log_text=log), MEMORY)

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
