"""Generic law tasks for independently managed repository workspaces."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import law
import luigi

from workflow_automation.cli import (
    DEFAULT_CONFIG,
    DEFAULT_WORKSPACE,
    BootstrapError,
    Repository,
    load_repositories,
    prepare_environment,
    prepare_repository,
    repository_is_current,
    run_git,
    run_program,
    validate_environment,
)


DEFAULT_PRODUCTIONS = DEFAULT_CONFIG.parent / "productions.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        return repository_is_current(repository, checkout)

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
        if not self.requires().complete():
            return False
        repository = self.repository_config()
        checkout = self.workspace_path() / repository.directory
        prefix = self.environment_root_path() / repository.directory
        return validate_environment(repository, checkout, prefix)

    def run(self) -> None:
        prepare_environment(
            self.repository_config(), self.workspace_path(), self.environment_root_path()
        )


class GridCredentialCheck(law.Task):
    """Read-only check for an existing CMS proxy with sufficient validity."""

    valid_for = luigi.Parameter(default="5:00", significant=False)

    def complete(self) -> bool:
        executable = shutil.which("voms-proxy-info")
        if not executable:
            return False
        try:
            run_program([executable, "--exists", "--valid", str(self.valid_for)])
        except BootstrapError:
            return False
        return True

    def run(self) -> None:
        raise BootstrapError(
            "a valid CMS proxy is required. Create it manually after sourcing the site grid "
            "setup, export X509_USER_PROXY, and rerun the task"
        )


class DitauSampleManifest(law.Task):
    """Discover dCache inputs into workflow-owned, fingerprinted JSON manifests."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def production_config(self) -> dict[str, object]:
        data = json.loads(Path(self.productions_config).read_text())
        try:
            production = data["productions"][str(self.production)]
        except KeyError as exc:
            raise BootstrapError(f"unknown production {self.production!r}") from exc
        if str(self.era) not in production["eras"]:
            raise BootstrapError(
                f"era {self.era!r} is not configured for production {self.production!r}"
            )
        return production

    def requires(self) -> dict[str, law.Task]:
        common = {
            "config": self.config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        return {
            "environment": RepositoryEnvironment(repository="HiggsDNA", **common),
            "credential": GridCredentialCheck(),
        }

    def state_dir(self) -> Path:
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "sample-manifests"
            / str(self.era)
        )

    def sample_dir(self) -> Path:
        return self.state_dir() / "samples"

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.state_dir() / "manifest.json"))

    def expected_names(self) -> list[str]:
        channels = self.production_config()["channels"]
        return ["samples_MC.json", *(f"samples_{channel}.json" for channel in channels)]

    def checkout(self) -> tuple[Repository, Path]:
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        repository = repositories["HiggsDNA"]
        checkout = Path(self.workspace).expanduser().resolve() / repository.directory
        return repository, checkout

    def current_fingerprint(self) -> str:
        production = self.production_config()
        _, checkout = self.checkout()
        inputs = [
            checkout / f"scripts/ditau/pre_processing/samples_{self.era}.yaml",
            checkout / "scripts/ditau/pre_processing/fetch_samples.py",
        ]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "analysis_type": production["analysis_type"],
                    "channels": production["channels"],
                    "era": str(self.era),
                },
                sort_keys=True,
            ).encode()
        )
        digest.update(run_git(["rev-parse", "HEAD"], cwd=checkout).encode())
        for path in inputs:
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def complete(self) -> bool:
        if not self.requires()["environment"].complete():
            return False
        if not self.output().exists():
            return False
        try:
            receipt = json.loads(Path(self.output().path).read_text())
            if receipt["input_fingerprint"] != self.current_fingerprint():
                return False
            expected = self.expected_names()
            if sorted(receipt["files"]) != sorted(expected):
                return False
            return all(
                (self.sample_dir() / name).is_file()
                and sha256_file(self.sample_dir() / name) == receipt["files"][name]
                for name in expected
            )
        except (BootstrapError, KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        production = self.production_config()
        repository, checkout = self.checkout()
        environment_base = (
            Path(self.environment_root).expanduser().resolve()
            if self.environment_root
            else Path(self.workspace).expanduser().resolve() / ".environments"
        )
        python = environment_base / repository.directory / "bin/python"
        script = checkout / "scripts/ditau/pre_processing/fetch_samples.py"
        self.sample_dir().mkdir(parents=True, exist_ok=True)
        run_program(
            [
                str(python),
                str(script),
                "--year",
                str(self.era),
                "--analysis-type",
                str(production["analysis_type"]),
                "--output-dir",
                str(self.sample_dir()),
                "--strict",
            ],
            cwd=checkout,
        )
        hashes = {}
        for name in self.expected_names():
            path = self.sample_dir() / name
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise BootstrapError(f"sample manifest is not a JSON object: {path}")
            hashes[name] = sha256_file(path)
        receipt = {
            "schema_version": 1,
            "production": str(self.production),
            "era": str(self.era),
            "analysis_type": production["analysis_type"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "higgsdna_commit": run_git(["rev-parse", "HEAD"], cwd=checkout),
            "input_fingerprint": self.current_fingerprint(),
            "files": hashes,
        }
        self.output().dump(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", formatter="text"
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
                            state
                            / "sample-manifests"
                            / str(era)
                            / "samples"
                            / f"samples_{channel}.json"
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


class DitauInputPreparation(law.WrapperTask):
    """Top-level non-submitting task for planning and dCache sample discovery."""

    production = luigi.Parameter(default="cp_2022_test")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> dict[str, object]:
        data = json.loads(Path(self.productions_config).read_text())
        try:
            production = data["productions"][str(self.production)]
        except KeyError as exc:
            raise BootstrapError(f"unknown production {self.production!r}") from exc
        common = {
            "production": self.production,
            "config": self.config,
            "productions_config": self.productions_config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        return {
            "plan": DitauProductionPlan(**common),
            "sample_manifests": [
                DitauSampleManifest(era=era, **common) for era in production["eras"]
            ],
        }
