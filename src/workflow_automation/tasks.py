"""Generic law tasks for independently managed repository workspaces."""

from __future__ import annotations

import hashlib
import json
import os
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

    @staticmethod
    def validate_sample_file(path: Path) -> None:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not data:
            raise BootstrapError(f"sample manifest must be a non-empty JSON object: {path}")
        for sample, files in data.items():
            if not isinstance(sample, str) or not sample:
                raise BootstrapError(f"sample manifest has an invalid dataset name: {path}")
            if not isinstance(files, list) or not files:
                raise BootstrapError(
                    f"sample {sample!r} must contain a non-empty list of files: {path}"
                )
            if any(not isinstance(item, str) or not item for item in files):
                raise BootstrapError(
                    f"sample {sample!r} contains a non-string or empty file path: {path}"
                )

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
                    "input_snapshot": production["input_snapshot"],
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
            for name in expected:
                path = self.sample_dir() / name
                if not path.is_file() or sha256_file(path) != receipt["files"][name]:
                    return False
                self.validate_sample_file(path)
            return True
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
            self.validate_sample_file(path)
            hashes[name] = sha256_file(path)
        receipt = {
            "schema_version": 1,
            "production": str(self.production),
            "era": str(self.era),
            "analysis_type": production["analysis_type"],
            "input_snapshot": production["input_snapshot"],
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


class DitauEffectiveEventPlan(law.Task):
    """Plan, but never execute, the two effective-event batch submissions."""

    # The fingerprint below covers the plan's inputs: configuration, the HiggsDNA
    # commit, and the sample manifest. It cannot cover the code that generates the
    # plan, so a change to what a command contains would otherwise leave an existing
    # plan looking current and silently keep using the old commands. Bump this
    # whenever the generated plan's structure or command content changes.
    SCHEMA_VERSION = 2

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> DitauInputPreparation:
        return DitauInputPreparation(
            production=self.production,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

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
        if not production.get("effective_output"):
            raise BootstrapError(
                f"production {self.production!r} has no effective_output configured"
            )
        return production

    def state_dir(self) -> Path:
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "effective-events"
            / str(self.era)
        )

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.state_dir() / "plan.json"))

    def checkout(self) -> tuple[Repository, Path]:
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        repository = repositories["HiggsDNA"]
        checkout = Path(self.workspace).expanduser().resolve() / repository.directory
        return repository, checkout

    def sample_receipt(self) -> Path:
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "sample-manifests"
            / str(self.era)
            / "manifest.json"
        )

    def current_fingerprint(self) -> str:
        production = self.production_config()
        _, checkout = self.checkout()
        digest = hashlib.sha256(Path(self.productions_config).read_bytes())
        digest.update(
            json.dumps(
                {
                    "era": str(self.era),
                    "effective_output": production["effective_output"],
                    "schema_version": self.SCHEMA_VERSION,
                },
                sort_keys=True,
            ).encode()
        )
        digest.update(run_git(["rev-parse", "HEAD"], cwd=checkout).encode())
        digest.update((checkout / "scripts/ditau/config/ditau_analysis.json").read_bytes())
        digest.update(self.sample_receipt().read_bytes())
        return digest.hexdigest()

    def complete(self) -> bool:
        if not self.requires().complete() or not self.output().exists():
            return False
        try:
            plan = json.loads(Path(self.output().path).read_text())
            if plan["input_fingerprint"] != self.current_fingerprint():
                return False
            expected = {"Events.json", "EventsNotSelected.json"}
            if set(plan["analysis_configs"]) != expected:
                return False
            config_dir = self.state_dir() / "analysis-configs"
            return all(
                (config_dir / name).is_file()
                and sha256_file(config_dir / name) == plan["analysis_configs"][name]
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
        run_analysis = checkout / "scripts/ditau/processing/run_analysis.py"
        sample_json = self.sample_receipt().parent / "samples" / "samples_MC.json"
        generated = self.state_dir() / "analysis-configs"
        manifests = self.state_dir() / "submission-records"
        generated.mkdir(parents=True, exist_ok=True)
        base = json.loads(
            (checkout / "scripts/ditau/config/ditau_analysis.json").read_text()
        )

        commands = []
        analysis_configs = {}
        for tree, events_not_selected in (("Events", False), ("EventsNotSelected", True)):
            analysis = dict(base)
            analysis.update(
                {
                    "samplejson": str(sample_json),
                    "year": str(self.era),
                    "Run_Effective": True,
                    "EventsNotSelected": events_not_selected,
                }
            )
            analysis_path = generated / f"{tree}.json"
            analysis_path.write_text(
                json.dumps(analysis, indent=2, sort_keys=True) + "\n"
            )
            analysis_configs[analysis_path.name] = sha256_file(analysis_path)
            commands.append(
                {
                    "stage": "effective-event-submission",
                    "era": str(self.era),
                    "tree": tree,
                    "submits_jobs": True,
                    "cwd": str(checkout),
                    # HTCondor submit files use `getenv = True`, and the generated job
                    # wrappers invoke a bare `python3`. The workers therefore inherit
                    # whichever interpreter is first on the submitting process's PATH.
                    # Submitting from the controller virtualenv sends workers a Python
                    # without the analysis stack, so record the environment the command
                    # must run under and prepend it at execution time.
                    "environment_bin": str(python.parent),
                    "argv": [
                        str(python),
                        str(run_analysis),
                        "--json-analysis",
                        str(analysis_path),
                        "--dump",
                        str(production["effective_output"]),
                        "--executor",
                        "imperial_condor",
                        "--channel",
                        "tt",
                        "--voms",
                        str(Path.home() / "cms.proxy"),
                        "--chunk",
                        "150000",
                        "--debug",
                        "--submission-manifest-dir",
                        str(manifests),
                    ],
                }
            )

        plan = {
            "schema_version": self.SCHEMA_VERSION,
            "production": str(self.production),
            "era": str(self.era),
            "effective_output": production["effective_output"],
            "sample_manifest_receipt": str(self.sample_receipt()),
            "higgsdna_commit": run_git(["rev-parse", "HEAD"], cwd=checkout),
            "input_fingerprint": self.current_fingerprint(),
            "analysis_configs": analysis_configs,
            "submission_enabled": False,
            "commands": commands,
        }
        self.output().dump(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", formatter="text"
        )


class DitauEffectiveEventReadiness(law.Task):
    """Validate effective-event submission prerequisites without submitting."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> DitauEffectiveEventPlan:
        return DitauEffectiveEventPlan(
            production=self.production,
            era=self.era,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.requires().state_dir() / "readiness.json"))

    def plan(self) -> dict[str, object]:
        return json.loads(Path(self.requires().output().path).read_text())

    def prerequisites_ready(self) -> bool:
        try:
            command = self.plan()["commands"][0]
            run_program([command["argv"][0], command["argv"][1], "--help"], cwd=Path(command["cwd"]))
        except (BootstrapError, KeyError, IndexError, OSError):
            return False
        return (
            GridCredentialCheck().complete()
            and shutil.which("condor_submit") is not None
            and shutil.which("condor_q") is not None
        )

    def complete(self) -> bool:
        if not self.requires().complete() or not self.prerequisites_ready():
            return False
        if not self.output().exists():
            return False
        try:
            report = json.loads(Path(self.output().path).read_text())
            plan = self.plan()
            return report["plan_fingerprint"] == plan["input_fingerprint"]
        except (KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        missing = [name for name in ("condor_submit", "condor_q") if not shutil.which(name)]
        if missing:
            raise BootstrapError(f"missing HTCondor tools: {', '.join(missing)}")
        if not GridCredentialCheck().complete():
            raise BootstrapError("a CMS proxy valid for at least 5 hours is required")
        plan = self.plan()
        commands = plan["commands"]
        if plan.get("submission_enabled") is not False or len(commands) != 2:
            raise BootstrapError("effective-event plan has an unexpected safety state")
        expected_trees = {"Events", "EventsNotSelected"}
        if {command.get("tree") for command in commands} != expected_trees:
            raise BootstrapError("effective-event plan does not contain exactly the two trees")
        for command in commands:
            argv = command["argv"]
            if (
                not command.get("submits_jobs")
                or "--executor" not in argv
                or argv[argv.index("--executor") + 1] != "imperial_condor"
            ):
                raise BootstrapError("effective-event command failed executor validation")
        run_program(
            [commands[0]["argv"][0], commands[0]["argv"][1], "--help"],
            cwd=Path(commands[0]["cwd"]),
        )
        report = {
            "schema_version": 1,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "plan_fingerprint": plan["input_fingerprint"],
            "checks": {
                "checkout_and_environment": True,
                "sample_manifest": True,
                "analysis_configs": True,
                "analysis_entrypoint": True,
                "cms_proxy": True,
                "condor_submit": True,
                "condor_q": True,
                "submission_enabled": False,
            },
        }
        self.output().dump(
            json.dumps(report, indent=2, sort_keys=True) + "\n", formatter="text"
        )


class DitauEffectiveEventSubmission(law.Task):
    """Explicitly submit one effective-event tree with interruption protection."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    tree = luigi.ChoiceParameter(choices=("Events", "EventsNotSelected"))
    allow_submission = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> DitauEffectiveEventReadiness:
        return DitauEffectiveEventReadiness(
            production=self.production,
            era=self.era,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def output(self) -> law.LocalFileTarget:
        state = self.requires().requires().state_dir()
        return law.LocalFileTarget(str(state / "submission-receipts" / f"{self.tree}.json"))

    def intent_path(self) -> Path:
        return self.requires().requires().state_dir() / "submission-intents" / f"{self.tree}.json"

    def command_output_path(self) -> Path:
        return (
            self.requires().requires().state_dir()
            / "submission-intents"
            / f"{self.tree}.command-output.log"
        )

    def plan(self) -> dict[str, object]:
        return self.requires().plan()

    def command(self) -> dict[str, object]:
        matches = [item for item in self.plan()["commands"] if item["tree"] == self.tree]
        if len(matches) != 1:
            raise BootstrapError(f"plan has no unique command for tree {self.tree}")
        return matches[0]

    @staticmethod
    def command_environment(command: dict[str, object]) -> dict[str, str] | None:
        """Put the command's own environment first on PATH for inherited job environments."""
        environment_bin = command.get("environment_bin")
        if not environment_bin:
            return None
        environment = dict(os.environ)
        path = environment.get("PATH", "")
        entries = [entry for entry in path.split(os.pathsep) if entry and entry != environment_bin]
        environment["PATH"] = os.pathsep.join([str(environment_bin), *entries])
        return environment

    def command_fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.command(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def complete(self) -> bool:
        if not self.output().exists():
            return False
        try:
            receipt = json.loads(Path(self.output().path).read_text())
            plan = self.plan()
            record = Path(receipt["submission_record"])
            return (
                receipt["plan_fingerprint"] == plan["input_fingerprint"]
                and receipt["command_fingerprint"] == self.command_fingerprint()
                and record.is_file()
                and sha256_file(record) == receipt["submission_record_sha256"]
            )
        except (BootstrapError, KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        if not self.allow_submission:
            raise BootstrapError(
                "submission is disabled; rerun with --allow-submission only after reviewing plan.json"
            )
        if not self.requires().complete():
            raise BootstrapError("effective-event submission readiness is no longer valid")
        intent = self.intent_path()
        if intent.exists():
            raise BootstrapError(
                f"submission intent already exists at {intent}; inspect Condor and reconcile it "
                "manually before any retry"
            )
        command = self.command()
        command_fingerprint = self.command_fingerprint()
        intent.parent.mkdir(parents=True, exist_ok=True)
        temporary = intent.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "production": str(self.production),
                    "era": str(self.era),
                    "tree": str(self.tree),
                    "plan_fingerprint": self.plan()["input_fingerprint"],
                    "command_fingerprint": command_fingerprint,
                    "status": "started",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        os.replace(temporary, intent)

        try:
            manifest_dir_index = command["argv"].index("--submission-manifest-dir") + 1
            manifest_dir = Path(command["argv"][manifest_dir_index])
            pattern = f"*__{self.era}__tt__{self.tree}.json"
            before = {
                path: sha256_file(path) for path in manifest_dir.glob(pattern) if path.is_file()
            }
            output = run_program(
                command["argv"],
                cwd=Path(command["cwd"]),
                env=self.command_environment(command),
            )
            after = [path for path in manifest_dir.glob(pattern) if path.is_file()]
            changed = [path for path in after if before.get(path) != sha256_file(path)]
            if len(changed) != 1:
                transcript = self.command_output_path()
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(output + "\n")
                raise BootstrapError(
                    f"submission command returned but found {len(changed)} new or changed records; "
                    f"the command exited successfully, so its captured output was written to "
                    f"{transcript}; intent retained at {intent} for manual reconciliation"
                )
        except Exception as exc:
            failed_intent = json.loads(intent.read_text())
            failed_intent.update(
                {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            if self.command_output_path().is_file():
                failed_intent["command_output"] = str(self.command_output_path())
            temporary.write_text(
                json.dumps(failed_intent, indent=2, sort_keys=True) + "\n"
            )
            os.replace(temporary, intent)
            raise
        record = changed[0].resolve()
        receipt = {
            "schema_version": 1,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "tree": str(self.tree),
            "plan_fingerprint": self.plan()["input_fingerprint"],
            "command_fingerprint": command_fingerprint,
            "submission_record": str(record),
            "submission_record_sha256": sha256_file(record),
        }
        self.output().dump(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", formatter="text"
        )
        completed_intent = json.loads(intent.read_text())
        completed_intent["status"] = "completed"
        completed_intent["submission_receipt"] = self.output().path
        intent.write_text(json.dumps(completed_intent, indent=2, sort_keys=True) + "\n")


class DitauEffectiveEventSubmissions(law.WrapperTask):
    """Submit both effective-event trees only with explicit operator opt-in."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    allow_submission = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> list[DitauEffectiveEventSubmission]:
        common = {
            "production": self.production,
            "era": self.era,
            "allow_submission": self.allow_submission,
            "config": self.config,
            "productions_config": self.productions_config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        return [
            DitauEffectiveEventSubmission(tree=tree, **common)
            for tree in ("Events", "EventsNotSelected")
        ]


class DitauEffectiveEventStatus(law.Task):
    """Report what happened to a submitted tree's jobs without changing anything.

    A submission receipt proves that jobs were submitted. It says nothing about
    whether they ran. This task closes that gap and never submits, resubmits, or
    moves anything.

    A job counts as completed when its standard output contains HiggsDNA's own
    completion marker, so this agrees with what `resubmit_jobs_condor.py` would
    decide rather than inventing a second, competing definition of success.
    """

    COMPLETION_MARKER = "Processing 100%"
    TAIL_BYTES = 256_000

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    tree = luigi.ChoiceParameter(choices=("Events", "EventsNotSelected"))
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def state_dir(self) -> Path:
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "effective-events"
            / str(self.era)
        )

    def receipt_path(self) -> Path:
        return self.state_dir() / "submission-receipts" / f"{self.tree}.json"

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.state_dir() / "status" / f"{self.tree}.json"))

    def complete(self) -> bool:
        # Job state changes underneath us, so a previous report never means the
        # current one is known. Always re-probe.
        return False

    def jobs_directory(self) -> Path:
        receipt_path = self.receipt_path()
        if not receipt_path.is_file():
            raise BootstrapError(
                f"no submission receipt for tree {self.tree!r} at {receipt_path}; "
                "there is nothing to report on until a submission has been receipted"
            )
        receipt = json.loads(receipt_path.read_text())
        record = Path(receipt["submission_record"])
        if not record.is_file():
            raise BootstrapError(f"submission record is missing: {record}")
        return Path(json.loads(record.read_text())["jobs_dir"])

    @classmethod
    def marker_present(cls, path: Path) -> bool:
        """Look for the completion marker in the tail, as HiggsDNA itself does."""
        try:
            with path.open("rb") as stream:
                try:
                    stream.seek(-cls.TAIL_BYTES, os.SEEK_END)
                except OSError:
                    stream.seek(0)
                return cls.COMPLETION_MARKER.encode() in stream.read()
        except OSError:
            return False

    @staticmethod
    def queued_job_count(jobs_dir: Path) -> int | None:
        """Count this tree's jobs still in the queue, or None if Condor cannot be read.

        Matching is by executable path. A bare owner query would also count the
        operator's unrelated work on the same schedd.
        """
        if not shutil.which("condor_q"):
            return None
        try:
            listing = run_program(["condor_q", os.environ.get("USER", ""), "-af", "Cmd"])
        except BootstrapError:
            return None
        prefix = str(jobs_dir)
        return sum(1 for line in listing.splitlines() if line.strip().startswith(prefix))

    def classify(self, jobs_dir: Path) -> dict[str, object]:
        datasets: dict[str, dict[str, int]] = {}
        failures: list[dict[str, object]] = []
        for submit_file in sorted(jobs_dir.glob("*.sub")):
            job_id = submit_file.stem
            expected = None
            for line in submit_file.read_text().splitlines():
                if line.startswith("queue"):
                    parts = line.split()
                    expected = int(parts[1]) if len(parts) > 1 else 1
                    break
            if expected is None:
                raise BootstrapError(f"no queue line in submit file: {submit_file}")

            counts = {"expected": expected, "completed": 0, "failed": 0, "pending": 0}
            for index in range(expected):
                # Condor writes <job>.<cluster>.<proc>.out; a resubmitted job adds
                # another cluster, so take the most recent attempt.
                candidates = sorted(
                    jobs_dir.glob(f"{job_id}.*.{index}.out"),
                    key=lambda item: item.stat().st_mtime,
                )
                if not candidates:
                    counts["pending"] += 1
                elif self.marker_present(candidates[-1]):
                    counts["completed"] += 1
                else:
                    counts["failed"] += 1
                    failures.append({"job": job_id, "proc": index, "output": str(candidates[-1])})
            datasets[job_id] = counts

        totals = {key: 0 for key in ("expected", "completed", "failed", "pending")}
        for counts in datasets.values():
            for key in totals:
                totals[key] += counts[key]
        return {"datasets": datasets, "totals": totals, "failures": failures}

    def run(self) -> None:
        jobs_dir = self.jobs_directory()
        classified = self.classify(jobs_dir)
        totals = classified["totals"]
        queued = self.queued_job_count(jobs_dir)
        report = {
            "schema_version": 1,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "tree": str(self.tree),
            "jobs_dir": str(jobs_dir),
            "queued_now": queued,
            "totals": totals,
            "datasets": classified["datasets"],
            "failures": classified["failures"],
        }
        self.output().dump(
            json.dumps(report, indent=2, sort_keys=True) + "\n", formatter="text"
        )
        pending = totals["pending"]
        print(
            f"[status] {self.tree}: {totals['completed']}/{totals['expected']} completed, "
            f"{totals['failed']} failed, {pending} pending"
            + (f", {queued} still queued" if queued is not None else ", queue unreadable")
        )


class DitauEffectiveEventResubmission(law.Task):
    """Resubmit only the jobs that did not finish, with explicit operator opt-in.

    Completeness here is defined by the jobs themselves, not by having run a
    command. The task is complete when the status report shows nothing left to
    fix, so a resubmission that silently achieved nothing cannot look like
    success the way a receipt-based check would.
    """

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    tree = luigi.ChoiceParameter(choices=("Events", "EventsNotSelected"))
    allow_submission = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> DitauEffectiveEventStatus:
        # Status never reports itself complete, so this always re-probes the jobs
        # before anything is resubmitted.
        return DitauEffectiveEventStatus(
            production=self.production,
            era=self.era,
            tree=self.tree,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def status(self) -> DitauEffectiveEventStatus:
        return self.requires()

    def state_dir(self) -> Path:
        return self.status().state_dir()

    def intent_path(self) -> Path:
        return self.state_dir() / "resubmission-intents" / f"{self.tree}.json"

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(
            str(self.state_dir() / "resubmission-receipts" / f"{self.tree}.json")
        )

    def live_state(self) -> tuple[Path, dict[str, object]] | None:
        """Classify the jobs as they are now, rather than trusting a written report.

        A status report on disk describes whatever was true when it was written.
        Deciding completeness from it would let a stale artifact stand in for the
        jobs themselves, which is exactly how a submitted-but-dead fleet once
        looked healthy. Pay the directory scan instead.
        """
        probe = self.status()
        try:
            jobs_dir = probe.jobs_directory()
        except (BootstrapError, KeyError, OSError, json.JSONDecodeError):
            return None
        if not jobs_dir.is_dir():
            return None
        return jobs_dir, probe.classify(jobs_dir)

    @staticmethod
    def outstanding(classified: dict[str, object]) -> int:
        totals = classified["totals"]
        return int(totals["failed"]) + int(totals["pending"])

    def complete(self) -> bool:
        state = self.live_state()
        if state is None:
            return False
        return self.outstanding(state[1]) == 0

    def plan_command(self) -> dict[str, object]:
        plan_path = self.state_dir() / "plan.json"
        if not plan_path.is_file():
            raise BootstrapError(f"effective-event plan is missing: {plan_path}")
        matches = [
            item
            for item in json.loads(plan_path.read_text())["commands"]
            if item["tree"] == str(self.tree)
        ]
        if len(matches) != 1:
            raise BootstrapError(f"plan has no unique command for tree {self.tree}")
        return matches[0]

    def run(self) -> None:
        if not self.allow_submission:
            raise BootstrapError(
                "resubmission is disabled; review the status report and rerun with "
                "--allow-submission only if the failed jobs should be resubmitted"
            )
        state = self.live_state()
        if state is None:
            raise BootstrapError(
                "cannot inspect the jobs for this tree; a receipted submission and its "
                "job directory must exist before anything can be resubmitted"
            )
        jobs_dir, classified = state
        outstanding = self.outstanding(classified)
        if outstanding == 0:
            raise BootstrapError("nothing to resubmit; no job is failed or pending")

        intent = self.intent_path()
        if intent.exists():
            raise BootstrapError(
                f"resubmission intent already exists at {intent}; inspect Condor and the "
                "status report, then reconcile it manually before any retry"
            )

        command = self.plan_command()
        checkout = Path(command["cwd"])
        environment_bin = command.get("environment_bin")
        script = checkout / "scripts/ditau/processing/resubmit_jobs_condor.py"
        if not script.is_file():
            raise BootstrapError(f"resubmission script is missing: {script}")
        argv = [str(Path(str(environment_bin)) / "python"), str(script), str(jobs_dir)]

        intent.parent.mkdir(parents=True, exist_ok=True)
        temporary = intent.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "production": str(self.production),
                    "era": str(self.era),
                    "tree": str(self.tree),
                    "jobs_dir": str(jobs_dir),
                    "outstanding_before": outstanding,
                    "totals_before": classified["totals"],
                    "status": "started",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        os.replace(temporary, intent)

        try:
            # The generated submit files use `getenv = True`, so the resubmitted
            # jobs inherit this PATH exactly as the original submission did.
            output = run_program(
                argv,
                cwd=checkout,
                env=DitauEffectiveEventSubmission.command_environment(command),
            )
            transcript = intent.with_name(f"{self.tree}.command-output.log")
            transcript.write_text(output + "\n")
        except Exception as exc:
            failed = json.loads(intent.read_text())
            failed.update(
                {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            temporary.write_text(json.dumps(failed, indent=2, sort_keys=True) + "\n")
            os.replace(temporary, intent)
            raise

        receipt = {
            "schema_version": 1,
            "resubmitted_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "tree": str(self.tree),
            "jobs_dir": str(jobs_dir),
            "outstanding_before": outstanding,
            "totals_before": classified["totals"],
            "command_output": str(transcript),
            # Deliberately no "after" counts. The resubmitted jobs have only just
            # been queued, so any success claim here would be about submission
            # again rather than about the jobs running. Rerun the status task.
        }
        self.output().dump(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", formatter="text"
        )
        completed = json.loads(intent.read_text())
        completed["status"] = "completed"
        completed["resubmission_receipt"] = self.output().path
        intent.write_text(json.dumps(completed, indent=2, sort_keys=True) + "\n")
        print(
            f"[resubmit] {self.tree}: {outstanding} outstanding job(s) resubmitted. "
            "Rerun DitauEffectiveEventStatus to see whether they ran."
        )
