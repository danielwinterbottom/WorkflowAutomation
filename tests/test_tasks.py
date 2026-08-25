import unittest
from unittest.mock import patch

from workflow_automation.tasks import (
    DitauProductionPlan,
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
