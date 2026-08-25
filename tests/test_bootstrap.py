from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_automation.cli import (
    BootstrapError,
    bootstrap,
    load_repositories,
    repository_is_current,
)


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
        self.initial_commit = git("rev-parse", "HEAD", cwd=self.source)

        self.workspace = self.root / "workspaces"
        self.config = self.root / "repositories.json"
        self.config.write_text(
            json.dumps(
                {
                    "repositories": {
                        "HiggsDNA": {
                            "url": str(self.source),
                            "revision": "master",
                            "commit": self.initial_commit,
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

    def test_clone_uses_pin_when_remote_branch_is_newer(self) -> None:
        (self.source / "README.md").write_text("newer unapproved fixture\n")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "newer unapproved fixture", cwd=self.source)

        bootstrap(self.config, self.workspace, [])
        checkout = self.workspace / "HiggsDNA"

        self.assertEqual(git("rev-parse", "HEAD", cwd=checkout), self.initial_commit)
        self.assertEqual(git("branch", "--show-current", cwd=checkout), "master")

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

    def test_fast_forwards_clean_checkout_to_new_pinned_commit(self) -> None:
        bootstrap(self.config, self.workspace, [])
        checkout = self.workspace / "HiggsDNA"
        (self.source / "README.md").write_text("updated fixture\n")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "update fixture", cwd=self.source)
        updated_commit = git("rev-parse", "HEAD", cwd=self.source)
        configuration = json.loads(self.config.read_text())
        configuration["repositories"]["HiggsDNA"]["commit"] = updated_commit
        self.config.write_text(json.dumps(configuration))

        repository = load_repositories(self.config)[0]
        self.assertFalse(repository_is_current(repository, checkout))

        bootstrap(self.config, self.workspace, [])

        self.assertEqual(git("rev-parse", "HEAD", cwd=checkout), updated_commit)
        self.assertTrue(repository_is_current(repository, checkout))

    def test_refuses_to_update_checkout_with_local_changes(self) -> None:
        bootstrap(self.config, self.workspace, [])
        checkout = self.workspace / "HiggsDNA"
        (checkout / "local.txt").write_text("do not overwrite\n")
        (self.source / "README.md").write_text("updated fixture\n")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "update fixture", cwd=self.source)
        updated_commit = git("rev-parse", "HEAD", cwd=self.source)
        configuration = json.loads(self.config.read_text())
        configuration["repositories"]["HiggsDNA"]["commit"] = updated_commit
        self.config.write_text(json.dumps(configuration))

        with self.assertRaisesRegex(BootstrapError, "local changes"):
            bootstrap(self.config, self.workspace, [])

        self.assertEqual(git("rev-parse", "HEAD", cwd=checkout), self.initial_commit)
        self.assertTrue((checkout / "local.txt").is_file())


if __name__ == "__main__":
    unittest.main()
