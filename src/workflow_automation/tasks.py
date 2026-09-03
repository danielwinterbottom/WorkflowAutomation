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

from workflow_automation import batch, provenance
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
    analysis_type = luigi.OptionalParameter(
        default=None,
        description="override the production's signal selection, for artefacts shared "
        "across analyses",
    )
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

    def selected_analysis_type(self) -> str:
        if self.analysis_type:
            return str(self.analysis_type)
        return str(self.production_config()["analysis_type"])

    def state_dir(self) -> Path:
        base = (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "sample-manifests"
            / str(self.era)
        )
        # Only an override gets its own directory, so the production's own
        # manifest keeps the path everything already refers to.
        if self.analysis_type and str(self.analysis_type) != str(
            self.production_config()["analysis_type"]
        ):
            return base.with_name(f"{self.era}__{self.analysis_type}")
        return base

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
                    "analysis_type": self.selected_analysis_type(),
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
                self.selected_analysis_type(),
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

    # As for the effective-event plan: the fingerprint covers the plan's inputs,
    # not the code that builds it, so a change to what a command contains would
    # otherwise leave an existing plan looking current. Bump when that changes.
    SCHEMA_VERSION = 2

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
        digest.update(str(self.SCHEMA_VERSION).encode())
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
                # run.py rewrites this file in place before submitting, to point
                # samplejson at the channel it is running. It already points there,
                # so the content is unchanged, but it is rewritten with json.dump's
                # own formatting. Match that exactly and the rewrite is a no-op
                # rather than something that makes the plan's own file look edited.
                target = generated / f"{era}__{channel}.json"
                target.write_text(json.dumps(analysis, indent=4))
                commands.append(
                    {
                        "stage": "standard-analysis",
                        "era": era,
                        "channel": channel,
                        "submits_jobs": True,
                        "cwd": str(checkout),
                        # The generated job wrappers invoke a bare python3 under
                        # `getenv = True`, so the workers take their interpreter
                        # from the PATH this process submits with.
                        "environment_bin": str(python.parent),
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
            "schema_version": self.SCHEMA_VERSION,
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

    def effective_analysis_type(self) -> str:
        """Which signal samples the counts cover.

        Effective event counts are a property of the samples rather than of the
        analysis: the sum of generator weights for a dataset is the same number
        whichever analysis reads it, and the counts file is shared between them.
        So it is discovered over every signal sample, while analysis_type keeps
        governing which samples the standard analysis actually processes.
        """
        production = self.production_config()
        return str(production.get("effective_analysis_type", production["analysis_type"]))

    def sample_manifest_dirname(self) -> str:
        production = self.production_config()
        selected = self.effective_analysis_type()
        if selected != str(production["analysis_type"]):
            return f"{self.era}__{selected}"
        return str(self.era)

    def requires(self) -> dict[str, law.Task]:
        common = {
            "production": self.production,
            "config": self.config,
            "productions_config": self.productions_config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        required: dict[str, law.Task] = {"inputs": DitauInputPreparation(**common)}
        if self.effective_analysis_type() != str(self.production_config()["analysis_type"]):
            required["samples"] = DitauSampleManifest(
                era=self.era, analysis_type=self.effective_analysis_type(), **common
            )
        return required

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
            / self.sample_manifest_dirname()
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
        if not all(item.complete() for item in self.requires().values()):
            return False
        if not self.output().exists():
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
    skip_completed = luigi.BoolParameter(
        default=False,
        significant=False,
        description="omit datasets whose jobs already finished in an earlier submission",
    )
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

    def completed_datasets(self) -> set[str]:
        """Datasets whose every job finished in an earlier submission of this tree.

        Widening a production should cost the jobs it adds, not the ones already
        done. HiggsDNA submits one cluster per dataset, so completeness is
        naturally per dataset, and the job names carry the dataset after an
        AN- prefix.
        """
        probe = DitauEffectiveEventStatus(
            production=self.production,
            era=self.era,
            tree=self.tree,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )
        # Every submission record for this tree, not just the current receipt.
        # Receipts get archived during reconciliation, and depending on one meant
        # that tidying up the bookkeeping silently destroyed the evidence of what
        # had already run, so a widened production resubmitted all of it.
        records = sorted(
            (probe.state_dir() / "submission-records").glob(f"*__{self.era}__tt__{self.tree}.json")
        )
        done: set[str] = set()
        for record in records:
            try:
                jobs_dir = Path(json.loads(record.read_text())["jobs_dir"])
            except (KeyError, OSError, json.JSONDecodeError):
                continue
            if not jobs_dir.is_dir():
                continue
            for job_id, counts in probe.classify(jobs_dir)["datasets"].items():
                if counts["completed"] == counts["expected"] and counts["expected"] > 0:
                    done.add(job_id[3:] if job_id.startswith("AN-") else job_id)
        return done

    def narrow_to_outstanding(self, command: dict[str, object]) -> tuple[dict[str, object], list[str]]:
        """Point the command at a sample list holding only the unfinished datasets.

        The sample list is named inside the analysis configuration rather than on
        the command line, so both are rewritten alongside the originals. They are
        written, not edited in place, so the plan's own files stay exactly as the
        plan describes them.
        """
        done = self.completed_datasets()
        if not done:
            return command, []

        analysis_index = command["argv"].index("--json-analysis") + 1
        analysis_path = Path(command["argv"][analysis_index])
        analysis = json.loads(analysis_path.read_text())
        manifest_path = Path(analysis["samplejson"])
        manifest = json.loads(manifest_path.read_text())

        skipped = sorted(name for name in manifest if name in done)
        outstanding = {name: files for name, files in manifest.items() if name not in done}
        if not outstanding:
            raise BootstrapError(
                f"every dataset for tree {self.tree} already has complete output; there is "
                "nothing to submit. Rerun without --skip-completed to submit them again"
            )

        reduced_manifest = manifest_path.with_name(f"{manifest_path.stem}.outstanding.json")
        reduced_manifest.write_text(json.dumps(outstanding, indent=2, sort_keys=True) + "\n")
        analysis["samplejson"] = str(reduced_manifest)
        reduced_analysis = analysis_path.with_name(f"{analysis_path.stem}.outstanding.json")
        reduced_analysis.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")

        argv = list(command["argv"])
        argv[analysis_index] = str(reduced_analysis)
        return {**command, "argv": argv}, skipped

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
        skipped: list[str] = []
        if self.skip_completed:
            command, skipped = self.narrow_to_outstanding(command)
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
                    # The plan describes every dataset; this records what was
                    # actually asked of the farm on this attempt.
                    "skipped_datasets": skipped,
                    "executed_argv": command["argv"],
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
            "skipped_datasets": skipped,
            "executed_argv": command["argv"],
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
                    failures.append(self.diagnose(jobs_dir, job_id, index, candidates[-1]))
            datasets[job_id] = counts

        totals = {key: 0 for key in ("expected", "completed", "failed", "pending")}
        for counts in datasets.values():
            for key in totals:
                totals[key] += counts[key]
        causes: dict[str, int] = {}
        for failure in failures:
            causes[failure["cause"]] = causes.get(failure["cause"], 0) + 1
        return {
            "datasets": datasets,
            "totals": totals,
            "causes": causes,
            "failures": failures,
        }

    @staticmethod
    def read_text(path: Path, limit: int = 256_000) -> str:
        try:
            with path.open("rb") as stream:
                try:
                    stream.seek(-limit, os.SEEK_END)
                except OSError:
                    stream.seek(0)
                return stream.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def diagnose(self, jobs_dir: Path, job_id: str, index: int, output: Path) -> dict[str, object]:
        """Record not just that a job failed, but why, so a retry can be reasoned about."""
        # <job>.<cluster>.<proc>.out, and the cluster's log covers every proc in it.
        parts = output.name.split(".")
        cluster = parts[-3] if len(parts) >= 4 else ""
        error = jobs_dir / f"{job_id}.{cluster}.{index}.err"
        log = jobs_dir / f"{job_id}.{cluster}.log"
        events = batch.events_for_proc(self.read_text(log), index) if log.is_file() else ""
        cause = batch.classify_failure(
            log_text=events,
            stderr_text=self.read_text(error) if error.is_file() else "",
            stdout_text=self.read_text(output),
        )
        return {
            "job": job_id,
            "proc": index,
            "cause": cause,
            "retryable": cause in batch.RETRYABLE,
            "output": str(output),
            "error": str(error) if error.is_file() else None,
            "log": str(log) if log.is_file() else None,
        }

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
            "causes": classified["causes"],
            "datasets": classified["datasets"],
            "failures": classified["failures"],
        }
        self.output().dump(
            json.dumps(report, indent=2, sort_keys=True) + "\n", formatter="text"
        )
        pending = totals["pending"]
        summary = ", ".join(
            f"{count} {cause}" for cause, count in sorted(classified["causes"].items())
        )
        print(
            f"[status] {self.tree}: {totals['completed']}/{totals['expected']} completed, "
            f"{totals['failed']} failed, {pending} pending"
            + (f", {queued} still queued" if queued is not None else ", queue unreadable")
            + (f" | causes: {summary}" if summary else "")
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

    DEFAULT_BATCH_CONFIG = DEFAULT_CONFIG.parent / "batch.json"

    batch_config = luigi.Parameter(default=str(DEFAULT_BATCH_CONFIG), significant=False)
    site = luigi.Parameter(default="imperial", significant=False)

    def state_path(self) -> Path:
        return self.state_dir() / "resubmission-state" / f"{self.tree}.json"

    def load_state(self) -> dict[str, dict[str, object]]:
        path = self.state_path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text()).get("jobs", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, jobs: dict[str, dict[str, object]]) -> None:
        path = self.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "tree": str(self.tree), "jobs": jobs},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        os.replace(temporary, path)

    @staticmethod
    def submit_file_for(jobs_dir: Path, job_id: str, proc: int, slot, runtime_attribute: str) -> Path:
        """Write a submit file for one job, matching the original output naming.

        The names must match what the original submission produced, because the
        status task finds a job's latest attempt by globbing them and taking the
        newest. A retry that wrote elsewhere would look like it never ran.
        """
        target = jobs_dir / "workflow_resubmit" / f"{job_id}.{proc}.sub"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"executable = {jobs_dir / (job_id + '.sh')}",
            f"arguments = {proc}",
            f"output = {jobs_dir / (job_id + '.$(ClusterId).' + str(proc) + '.out')}",
            f"error = {jobs_dir / (job_id + '.$(ClusterId).' + str(proc) + '.err')}",
            f"log = {jobs_dir / (job_id + '.$(ClusterId).log')}",
            # The job wrapper invokes a bare python3, so the workers take their
            # interpreter from the PATH this process is submitting with.
            "getenv = True",
            *slot.submit_lines(runtime_attribute),
            "queue 1",
        ]
        target.write_text("\n".join(lines) + "\n")
        return target

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

        site = batch.load_site(Path(str(self.batch_config)), str(self.site))
        intent = self.intent_path()
        if intent.exists():
            raise BootstrapError(
                f"resubmission intent already exists at {intent}; inspect Condor and the "
                "status report, then reconcile it manually before any retry"
            )

        command = self.plan_command()
        checkout = Path(command["cwd"])
        history = self.load_state()

        planned: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for failure in classified["failures"]:
            key = f"{failure['job']}:{failure['proc']}"
            record = history.get(key, {})
            attempts = int(record.get("attempts", 0))
            if attempts >= site.max_attempts:
                skipped.append({**failure, "reason": f"already retried {attempts} time(s)"})
                continue
            demand = batch.Demand(
                int(record.get("minimum_runtime_seconds", 0)),
                int(record.get("minimum_memory_mb", 0)),
            )
            cpus, memory, runtime = batch.read_submit_resources(
                jobs_dir / f"{failure['job']}.sub", site.runtime_attribute
            )
            try:
                slot, updated = site.escalate(
                    str(failure["cause"]), runtime, memory, demand
                )
            except BootstrapError as exc:
                skipped.append({**failure, "reason": str(exc)})
                continue
            planned.append(
                {"failure": failure, "key": key, "slot": slot, "demand": updated,
                 "attempts": attempts}
            )

        if not planned:
            raise BootstrapError(
                f"nothing can be resubmitted: {len(skipped)} outstanding job(s) are either "
                "not retryable, out of attempts, or beyond what this site can provide. "
                f"See the status report at {self.status().output().path}"
            )

        intent.parent.mkdir(parents=True, exist_ok=True)
        temporary = intent.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "production": str(self.production),
                    "era": str(self.era),
                    "tree": str(self.tree),
                    "jobs_dir": str(jobs_dir),
                    "outstanding_before": outstanding,
                    "totals_before": classified["totals"],
                    "causes_before": classified["causes"],
                    "planned": [
                        {"job": item["key"], "cause": item["failure"]["cause"],
                         "slot": item["slot"].describe()}
                        for item in planned
                    ],
                    "skipped": [
                        {"job": f"{item['job']}:{item['proc']}", "cause": item["cause"],
                         "reason": item["reason"]}
                        for item in skipped
                    ],
                    "status": "started",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        os.replace(temporary, intent)

        environment = DitauEffectiveEventSubmission.command_environment(command)
        submitted: list[dict[str, object]] = []
        try:
            for item in planned:
                failure = item["failure"]
                submit_file = self.submit_file_for(
                    jobs_dir, str(failure["job"]), int(failure["proc"]),
                    item["slot"], site.runtime_attribute,
                )
                run_program(["condor_submit", str(submit_file)], cwd=checkout, env=environment)
                history[str(item["key"])] = {
                    "attempts": item["attempts"] + 1,
                    "minimum_runtime_seconds": item["demand"].minimum_runtime_seconds,
                    "minimum_memory_mb": item["demand"].minimum_memory_mb,
                    "last_cause": failure["cause"],
                    "last_slot": item["slot"].describe(),
                    "last_resubmitted_at": datetime.now(timezone.utc).isoformat(),
                }
                submitted.append(
                    {"job": item["key"], "cause": failure["cause"],
                     "slot": item["slot"].describe(), "submit_file": str(submit_file)}
                )
        except Exception as exc:
            # Persist whatever did go out before recording the failure, so a
            # partial resubmission is not repeated on top of itself.
            self.save_state(history)
            failed = json.loads(intent.read_text())
            failed.update(
                {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "submitted_before_failure": submitted,
                }
            )
            temporary.write_text(json.dumps(failed, indent=2, sort_keys=True) + "\n")
            os.replace(temporary, intent)
            raise

        self.save_state(history)
        receipt = {
            "schema_version": 2,
            "resubmitted_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "tree": str(self.tree),
            "jobs_dir": str(jobs_dir),
            "site": str(self.site),
            "outstanding_before": outstanding,
            "totals_before": classified["totals"],
            "causes_before": classified["causes"],
            "submitted": submitted,
            "skipped": [
                {"job": f"{item['job']}:{item['proc']}", "cause": item["cause"],
                 "reason": item["reason"]}
                for item in skipped
            ],
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
            f"[resubmit] {self.tree}: {len(submitted)} job(s) resubmitted, "
            f"{len(skipped)} skipped. Rerun DitauEffectiveEventStatus to see whether they ran."
        )


class DitauDerivedArtefact(law.Task):
    """Base for the committed files derived from the effective-event counts.

    These are expensive to produce and are kept in the repository once made, so
    the question each subclass answers is not "have I run" but "is what is
    already here still derived from the inputs I would use". When it is, nothing
    runs, and in particular the thousands of effective-event jobs behind it do
    not need to be submitted at all.
    """

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    allow_overwrite = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    #: file produced under scripts/ditau/config/<era>/
    artefact_name: str = ""

    def secondary_artefacts(self) -> list[Path]:
        """Other files the same command writes.

        A task that produces more than one file must protect and stamp all of
        them. getParams writes a second params file for the filtered Drell-Yan
        samples, and because that was not declared it was overwritten without
        the guard ever being consulted.
        """
        return []

    def checkout(self) -> Path:
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        return Path(self.workspace).expanduser().resolve() / repositories["HiggsDNA"].directory

    def environment_python(self) -> Path:
        repositories = {item.name: item for item in load_repositories(Path(self.config))}
        base = (
            Path(self.environment_root).expanduser().resolve()
            if self.environment_root
            else Path(self.workspace).expanduser().resolve() / ".environments"
        )
        return base / repositories["HiggsDNA"].directory / "bin/python"

    def config_dir(self) -> Path:
        return self.checkout() / "scripts/ditau/config" / str(self.era)

    def artefact(self) -> Path:
        return self.config_dir() / self.artefact_name

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.artefact()))

    def sources(self) -> dict[str, Path]:
        raise NotImplementedError

    def expected_provenance(self) -> dict[str, object]:
        return provenance.describe(
            self.sources(),
            {
                "era": str(self.era),
                "production": str(self.production),
                "higgsdna_commit": run_git(["rev-parse", "HEAD"], cwd=self.checkout()),
            },
        )

    def complete(self) -> bool:
        try:
            expected = self.expected_provenance()
            if not provenance.matches(self.artefact(), expected):
                return False
            return all(
                provenance.matches(item, expected) for item in self.secondary_artefacts()
            )
        except (BootstrapError, OSError):
            return False

    def command(self) -> list[str]:
        raise NotImplementedError

    def run(self) -> None:
        artefact = self.artefact()
        produced = [artefact, *self.secondary_artefacts()]
        for item in produced:
            provenance.guard_overwrite(item, bool(self.allow_overwrite))
        expected = self.expected_provenance()
        environment = dict(os.environ)
        bin_dir = str(self.environment_python().parent)
        entries = [item for item in environment.get("PATH", "").split(os.pathsep) if item]
        environment["PATH"] = os.pathsep.join([bin_dir, *entries])
        run_program(self.command(), cwd=self.checkout(), env=environment)
        if not artefact.is_file():
            raise BootstrapError(f"{self.command()[1]} did not produce {artefact}")
        for item in produced:
            if item.is_file():
                provenance.stamp(item, expected)
                print(f"[derived] {self.era}: wrote {item}")


class DitauEffectiveEventCounts(DitauDerivedArtefact):
    """Step 3: sum the per-file effective-event counts into one file per sample.

    This is the boundary where the batch jobs stop being needed. If the counts
    already present were derived from the sample list and manifest still in use,
    nothing here runs and neither tree has to be submitted.
    """

    artefact_name = "effective_events.yaml"

    def production_config(self) -> dict[str, object]:
        data = json.loads(Path(self.productions_config).read_text())
        try:
            return data["productions"][str(self.production)]
        except KeyError as exc:
            raise BootstrapError(f"unknown production {self.production!r}") from exc

    def sample_manifest(self) -> Path:
        """The manifest the counts were actually produced from.

        This must follow the same selection as the effective-event plan. When
        they disagree the provenance header names inputs that did not produce
        the file, which is worse than having none: it reads as a guarantee.
        """
        production = self.production_config()
        selected = str(
            production.get("effective_analysis_type", production["analysis_type"])
        )
        directory = (
            f"{self.era}__{selected}"
            if selected != str(production["analysis_type"])
            else str(self.era)
        )
        return (
            Path(self.workspace).expanduser().resolve()
            / ".workflow_automation"
            / "productions"
            / str(self.production)
            / "sample-manifests"
            / directory
            / "samples"
            / "samples_MC.json"
        )

    def sources(self) -> dict[str, Path]:
        # What the counts actually depend on: which samples were asked for, and
        # which files were found for them. Not the analysis configuration, which
        # does not affect a count of generator weights.
        return {
            "samples_yaml": self.checkout()
            / f"scripts/ditau/pre_processing/samples_{self.era}.yaml",
            "sample_manifest": self.sample_manifest(),
            # The program too, not just the data. A fix to how weights are summed
            # changes these numbers while leaving every input identical, so
            # without this the counts would look current and stay wrong.
            "generator": self.checkout() / "scripts/ditau/processing/getEffectiveEvents.py",
        }

    def effective_output(self) -> Path:
        output = self.production_config().get("effective_output")
        if not output:
            raise BootstrapError(
                f"production {self.production!r} has no effective_output configured"
            )
        return Path(str(output))

    def run(self) -> None:
        # The counts are summed from what the jobs wrote, so refuse clearly if
        # they were never run rather than producing an empty or partial file.
        produced = self.checkout() / self.effective_output() / str(self.era)
        if not produced.is_dir() or not any(produced.iterdir()):
            raise BootstrapError(
                f"no effective-event output under {produced}. Submit both trees first with "
                "DitauEffectiveEventSubmission, or restore the counts file that already "
                "carried valid provenance"
            )
        super().run()

    def command(self) -> list[str]:
        return [
            str(self.environment_python()),
            "scripts/ditau/processing/getEffectiveEvents.py",
            "--directory",
            str(self.effective_output()),
            "--year",
            str(self.era),
        ]


class DitauStitching(DitauDerivedArtefact):
    """Step 4: combine cross sections with the effective-event counts."""

    artefact_name = "Stitching.yaml"

    def requires(self) -> DitauEffectiveEventCounts:
        return DitauEffectiveEventCounts(
            production=self.production,
            era=self.era,
            allow_overwrite=self.allow_overwrite,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def sources(self) -> dict[str, Path]:
        return {
            "cross_sections": self.checkout() / "scripts/ditau/config/cross_sections.yaml",
            "effective_events": self.config_dir() / "effective_events.yaml",
            "generator": self.checkout() / "scripts/ditau/processing/getStitchingInfo.py",
        }

    def command(self) -> list[str]:
        return [
            str(self.environment_python()),
            "scripts/ditau/processing/getStitchingInfo.py",
            "--year",
            str(self.era),
        ]


class DitauParams(DitauDerivedArtefact):
    """Step 5: assemble luminosity, cross sections and effective events."""

    artefact_name = "params.yaml"

    def requires(self) -> DitauEffectiveEventCounts:
        return DitauEffectiveEventCounts(
            production=self.production,
            era=self.era,
            allow_overwrite=self.allow_overwrite,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def sources(self) -> dict[str, Path]:
        return {
            "samples_yaml": self.checkout()
            / f"scripts/ditau/pre_processing/samples_{self.era}.yaml",
            "cross_sections": self.checkout() / "scripts/ditau/config/cross_sections.yaml",
            "effective_events": self.config_dir() / "effective_events.yaml",
            "filter_efficiencies": self.config_dir() / "filter_efficiencies.yaml",
            "generator": self.checkout() / "scripts/ditau/processing/getParams.py",
        }

    #: eras for which getParams also writes the filtered Drell-Yan params
    DY_FILTERED_ERAS = ("Run3_2022", "Run3_2022EE", "Run3_2023", "Run3_2023BPix")

    def secondary_artefacts(self) -> list[Path]:
        if str(self.era) in self.DY_FILTERED_ERAS:
            return [self.config_dir() / "params_DYfiltered.yaml"]
        return []

    def command(self) -> list[str]:
        # params.yaml is analysis specific, unlike the counts: the committed
        # Run3_2022 file holds no MSSM entries at all. So it takes the
        # production's own type, and passing it explicitly is also what stops
        # the script stopping to ask.
        production = json.loads(Path(self.productions_config).read_text())
        analysis_type = production["productions"][str(self.production)]["analysis_type"]
        return [
            str(self.environment_python()),
            "scripts/ditau/processing/getParams.py",
            "--year",
            str(self.era),
            "--analysis-type",
            str(analysis_type),
        ]


class DitauStitchingAndParams(law.WrapperTask):
    """Both derived configuration files for one era."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    allow_overwrite = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> list[DitauDerivedArtefact]:
        common = {
            "production": self.production,
            "era": self.era,
            "allow_overwrite": self.allow_overwrite,
            "config": self.config,
            "productions_config": self.productions_config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        return [DitauStitching(**common), DitauParams(**common)]


class DitauStandardAnalysisReadiness(law.Task):
    """Validate the standard-analysis prerequisites without submitting anything."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> dict[str, law.Task]:
        common = {
            "production": self.production,
            "config": self.config,
            "productions_config": self.productions_config,
            "workspace": self.workspace,
            "environment_root": self.environment_root,
        }
        return {
            "plan": DitauProductionPlan(**common),
            # The standard analysis reads the stitching and params built from the
            # effective-event counts, so they must exist and be current first.
            # When they already are, nothing upstream runs.
            "stitching": DitauStitching(era=self.era, **common),
            "params": DitauParams(era=self.era, **common),
        }

    def state_dir(self) -> Path:
        return self.requires()["plan"].state_dir()

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(str(self.state_dir() / f"standard-readiness-{self.era}.json"))

    def plan(self) -> dict[str, object]:
        return json.loads(Path(self.requires()["plan"].output().path).read_text())

    def commands(self) -> list[dict[str, object]]:
        return [
            item
            for item in self.plan()["commands"]
            if item["stage"] == "standard-analysis" and item["era"] == str(self.era)
        ]

    def prerequisites_ready(self) -> bool:
        try:
            commands = self.commands()
            if not commands:
                return False
            run_program(
                [commands[0]["argv"][0], commands[0]["argv"][1], "--help"],
                cwd=Path(commands[0]["cwd"]),
            )
        except (BootstrapError, KeyError, IndexError, OSError):
            return False
        return (
            GridCredentialCheck().complete()
            and shutil.which("condor_submit") is not None
            and shutil.which("condor_q") is not None
        )

    def complete(self) -> bool:
        if not all(item.complete() for item in self.requires().values()):
            return False
        if not self.output().exists() or not self.prerequisites_ready():
            return False
        try:
            report = json.loads(Path(self.output().path).read_text())
            return report["plan_fingerprint"] == self.plan()["input_fingerprint"]
        except (KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        missing = [name for name in ("condor_submit", "condor_q") if not shutil.which(name)]
        if missing:
            raise BootstrapError(f"missing HTCondor tools: {', '.join(missing)}")
        if not GridCredentialCheck().complete():
            raise BootstrapError("a CMS proxy valid for at least 5 hours is required")
        plan = self.plan()
        commands = self.commands()
        if not commands:
            raise BootstrapError(
                f"the plan has no standard-analysis command for era {self.era}"
            )
        expected = set(self.production_channels())
        if {str(item["channel"]) for item in commands} != expected:
            raise BootstrapError(
                "the plan's standard-analysis channels do not match the production"
            )
        for item in commands:
            if not item.get("submits_jobs") or not item.get("environment_bin"):
                raise BootstrapError(
                    f"standard-analysis command for {item['channel']} is missing its "
                    "environment; the workers would inherit the wrong interpreter"
                )
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
            "channels": sorted(expected),
            "checks": {
                "plan": True,
                "stitching_and_params": True,
                "analysis_entrypoint": True,
                "command_environment": True,
                "cms_proxy": True,
                "condor_submit": True,
                "condor_q": True,
                "submission_enabled": False,
            },
        }
        self.output().dump(
            json.dumps(report, indent=2, sort_keys=True) + "\n", formatter="text"
        )

    def production_channels(self) -> list[str]:
        data = json.loads(Path(self.productions_config).read_text())
        return list(data["productions"][str(self.production)]["channels"])


class DitauStandardAnalysisSubmission(law.Task):
    """Submit the standard analysis for one channel, with the usual safeguards."""

    production = luigi.Parameter(default="cp_2022_test")
    era = luigi.Parameter(default="Run3_2022")
    channel = luigi.Parameter()
    allow_submission = luigi.BoolParameter(default=False, significant=False)
    config = luigi.Parameter(default=str(DEFAULT_CONFIG), significant=False)
    productions_config = luigi.Parameter(default=str(DEFAULT_PRODUCTIONS), significant=False)
    workspace = luigi.Parameter(default=str(DEFAULT_WORKSPACE), significant=False)
    environment_root = luigi.OptionalParameter(default=None, significant=False)

    def requires(self) -> DitauStandardAnalysisReadiness:
        return DitauStandardAnalysisReadiness(
            production=self.production,
            era=self.era,
            config=self.config,
            productions_config=self.productions_config,
            workspace=self.workspace,
            environment_root=self.environment_root,
        )

    def state_dir(self) -> Path:
        return self.requires().state_dir()

    def output(self) -> law.LocalFileTarget:
        return law.LocalFileTarget(
            str(self.state_dir() / "standard-receipts" / f"{self.era}__{self.channel}.json")
        )

    def intent_path(self) -> Path:
        return self.state_dir() / "standard-intents" / f"{self.era}__{self.channel}.json"

    def command(self) -> dict[str, object]:
        matches = [
            item
            for item in self.requires().commands()
            if str(item["channel"]) == str(self.channel)
        ]
        if len(matches) != 1:
            raise BootstrapError(
                f"the plan has no unique standard-analysis command for channel {self.channel}"
            )
        return matches[0]

    def command_fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.command(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def complete(self) -> bool:
        if not self.output().exists():
            return False
        try:
            receipt = json.loads(Path(self.output().path).read_text())
            record = Path(receipt["submission_record"])
            return (
                receipt["plan_fingerprint"] == self.requires().plan()["input_fingerprint"]
                and receipt["command_fingerprint"] == self.command_fingerprint()
                and record.is_file()
                and sha256_file(record) == receipt["submission_record_sha256"]
            )
        except (BootstrapError, KeyError, OSError, json.JSONDecodeError):
            return False

    def run(self) -> None:
        if not self.allow_submission:
            raise BootstrapError(
                "submission is disabled; rerun with --allow-submission only after reviewing "
                f"{self.state_dir() / 'plan.json'}"
            )
        if not self.requires().complete():
            raise BootstrapError("standard-analysis readiness is no longer valid")
        intent = self.intent_path()
        if intent.exists():
            raise BootstrapError(
                f"submission intent already exists at {intent}; inspect Condor and reconcile "
                "it manually before any retry"
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
                    "channel": str(self.channel),
                    "plan_fingerprint": self.requires().plan()["input_fingerprint"],
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
            index = command["argv"].index("--submission-manifest-dir") + 1
            manifest_dir = Path(command["argv"][index])
            pattern = f"*__{self.era}__{self.channel}__*.json"
            before = {
                path: sha256_file(path)
                for path in manifest_dir.glob(pattern)
                if path.is_file()
            }
            output = run_program(
                command["argv"],
                cwd=Path(command["cwd"]),
                env=DitauEffectiveEventSubmission.command_environment(command),
            )
            after = [path for path in manifest_dir.glob(pattern) if path.is_file()]
            changed = [path for path in after if before.get(path) != sha256_file(path)]
            if len(changed) != 1:
                transcript = intent.with_name(f"{self.era}__{self.channel}.command-output.log")
                transcript.write_text(output + "\n")
                raise BootstrapError(
                    f"submission command returned but found {len(changed)} new or changed "
                    f"records; the command exited successfully, so its captured output was "
                    f"written to {transcript}; intent retained at {intent}"
                )
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

        record = changed[0].resolve()
        receipt = {
            "schema_version": 1,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "production": str(self.production),
            "era": str(self.era),
            "channel": str(self.channel),
            "plan_fingerprint": self.requires().plan()["input_fingerprint"],
            "command_fingerprint": command_fingerprint,
            "submission_record": str(record),
            "submission_record_sha256": sha256_file(record),
        }
        self.output().dump(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", formatter="text"
        )
        completed = json.loads(intent.read_text())
        completed["status"] = "completed"
        completed["submission_receipt"] = self.output().path
        intent.write_text(json.dumps(completed, indent=2, sort_keys=True) + "\n")
        print(f"[standard] {self.era} {self.channel}: submitted, record {record.name}")
