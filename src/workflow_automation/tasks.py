"""Generic law tasks for independently managed repository workspaces."""

from __future__ import annotations

import hashlib
import json
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


DEFAULT_PRODUCTIONS = DEFAULT_CONFIG.parent / "productions.json"


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


class DitauProductionPlan(law.Task):
    """Build a non-submitting, inspectable plan for a configured ditau production."""

    production = luigi.Parameter(default="cp_2022_test")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> RepositoryEnvironment:
        return RepositoryEnvironment(
            repository="HiggsDNA",
            config=self.config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def state_dir(self) -> Path:
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
        )

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.state_dir() / "plan.json"))

    def production_config(self) -> dict[str, object]:
        data = json.loads(Path(self.productions_config).read_text())
        try:
            return data["productions"][str(self.production)]
        except KeyError as exc:
            raise BootstrapError(f"unknown production {self.production!r}") from exc

    def current_fingerprint(self) -> str:
        production = self.production_config()
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        checkout = (
            Path(self.workspace).expanduser().resolve()
            / repositories["HiggsDNA"].directory
        )
        digest = hashlib.sha256(Path(self.productions_config).read_bytes())
        digest.update(run_git(["rev-parse", "HEAD"], cwd=checkout).encode())
        for channel in production["channels"]:
            source = checkout / f"scripts/ditau/config/ditau_analysis_{channel}.json"
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def complete(self) -> bool:
        if not self.requires().complete():
            return False
        if not self.output().exists():
            return False
        try:
            plan = json.loads(Path(self.output().path).read_text())
            return plan["input_fingerprint"] == self.current_fingerprint()
        except (BootstrapError, KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        production = self.production_config()
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        repository = repositories["HiggsDNA"]
        checkout = Path(self.workspace).expanduser().resolve() / repository.directory
        environment_base = (
            Path(self.environment_root).expanduser().resolve()
            if self.environment_root
            else Path(self.workspace).expanduser().resolve() / ".environments"
        )
        python = environment_base / repository.directory / "bin/python"
        run_script = checkout / "scripts/ditau/processing/run.py"
        state = self.state_dir()
        generated = state / "analysis-configs"
        manifests = state / "submission-records"
        generated.mkdir(parents=True, exist_ok=True)

        commands = []
        for era in production["eras"]:
            for channel in production["channels"]:
                source = checkout / f"scripts/ditau/config/ditau_analysis_{channel}.json"
                analysis = json.loads(source.read_text())
                analysis.update(
                    {
                        "year": era,
                        "samplejson": str(
                            checkout
                            / f"scripts/ditau/config/{era}/samples/samples_{channel}.json"
                        ),
                        "Run_Effective": False,
                        "EventsNotSelected": False,
                    }
                )
                target = generated / f"{era}__{channel}.json"
                target.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")
                commands.append(
                    {
                        "stage": "standard-analysis",
                        "era": era,
                        "channel": channel,
                        "submits_jobs": True,
                        "argv": [
                            str(python),
                            str(run_script),
                            "--json-analysis",
                            str(target),
                            "--output",
                            str(production["output"]),
                            "--step",
                            "standard",
                            "--batch",
                            "--channels",
                            str(channel),
                            "--submission-manifest-dir",
                            str(manifests),
                        ],
                    }
                )

        plan = {
            "schema_version": 1,
            "production": self.production,
            "analysis_type": production["analysis_type"],
            "eras": production["eras"],
            "channels": production["channels"],
            "output": production["output"],
            "higgsdna_commit": run_git(["rev-parse", "HEAD"], cwd=checkout),
            "input_fingerprint": self.current_fingerprint(),
            "submission_enabled": False,
            "commands": commands,
        }
        self.output().dump(json.dumps(plan, indent=2, sort_keys=True) + "\n", formatter="text")
