from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_automation.cli import BootstrapError, bootstrap


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git("init", "-b", "master", cwd=self.source)
        git("config", "user.email", "test@example.invalid", cwd=self.source)
        git("config", "user.name", "Workflow Test", cwd=self.source)
        (self.source / "README.md").write_text("fixture\n")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "fixture", cwd=self.source)

        self.workspace = self.root / "workspaces"
        self.config = self.root / "repositories.json"
        self.config.write_text(
            json.dumps(
                {
                    "repositories": {
                        "HiggsDNA": {
                            "url": str(self.source),
                            "revision": "master",
                            "directory": "HiggsDNA",
                        }
                    }
                }
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_clones_missing_repository_and_is_repeatable(self) -> None:
        bootstrap(self.config, self.workspace, [])
        checkout = self.workspace / "HiggsDNA"
        first_commit = git("rev-parse", "HEAD", cwd=checkout)

        bootstrap(self.config, self.workspace, [])

        self.assertEqual(first_commit, git("rev-parse", "HEAD", cwd=checkout))

    def test_rejects_existing_non_repository_directory(self) -> None:
        destination = self.workspace / "HiggsDNA"
        destination.mkdir(parents=True)

        with self.assertRaises(BootstrapError):
            bootstrap(self.config, self.workspace, [])

    def test_rejects_incomplete_git_checkout(self) -> None:
        destination = self.workspace / "HiggsDNA"
        destination.mkdir(parents=True)
        git("init", cwd=destination)

        with self.assertRaisesRegex(BootstrapError, "incomplete checkout"):
            bootstrap(self.config, self.workspace, [])

    def test_rejects_unexpected_origin(self) -> None:
        bootstrap(self.config, self.workspace, [])
        checkout = self.workspace / "HiggsDNA"
        git("remote", "set-url", "origin", "https://example.invalid/wrong.git", cwd=checkout)

        with self.assertRaises(BootstrapError):
            bootstrap(self.config, self.workspace, [])


if __name__ == "__main__":
    unittest.main()
