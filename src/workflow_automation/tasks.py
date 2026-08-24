"""Generic law tasks for independently managed repository workspaces."""

from __future__ import annotations

from pathlib import Path

import law
import luigi

from workflow_automation.cli import (
    DEFAULT_CONFIG,
    DEFAULT_WORKSPACE,
    BootstrapError,
    Repository,
    load_repositories,
    normalize_git_url,
    prepare_environment,
    prepare_repository,
    run_git,
    validate_environment,
)


class RepositoryTask(law.Task):
    """Base parameters shared by tasks that operate on a configured repository."""

    repository = luigi.Parameter(description="repository name from repositories.json")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def repository_config(self) -> Repository:
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        try:
            return repositories[str(self.repository)]
        except KeyError as exc:
            known = ", ".join(sorted(repositories))
            raise BootstrapError(
                f"unknown repository {self.repository!r}; configured: {known}"
            ) from exc

    def workspace_path(self) -> Path:
        return Path(self.workspace).expanduser().resolve()

    def environment_root_path(self) -> Path:
        if self.environment_root:
            return Path(self.environment_root).expanduser().resolve()
        return self.workspace_path() / ".environments"


class RepositoryCheckout(RepositoryTask):
    """Clone a missing repository or accept an existing matching checkout."""

    def complete(self) -> bool:
        repository = self.repository_config()
        checkout = self.workspace_path() / repository.directory
        if not (checkout / ".git").exists():
            return False
        try:
            run_git(["rev-parse", "--verify", "HEAD"], cwd=checkout)
            origin = run_git(["remote", "get-url", "origin"], cwd=checkout)
        except BootstrapError:
            return False
        return normalize_git_url(origin) == normalize_git_url(repository.url)

    def run(self) -> None:
        prepare_repository(self.repository_config(), self.workspace_path())


class RepositoryEnvironment(RepositoryTask):
    """Create and validate only the selected repository's software environment."""

    def requires(self) -> RepositoryCheckout:
        return RepositoryCheckout(
            repository=self.repository,
            config=self.config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def complete(self) -> bool:
        repository = self.repository_config()
        checkout = self.workspace_path() / repository.directory
        prefix = self.environment_root_path() / repository.directory
        return validate_environment(repository, checkout, prefix)

    def run(self) -> None:
        prepare_environment(
            self.repository_config(), self.workspace_path(), self.environment_root_path()
        )
