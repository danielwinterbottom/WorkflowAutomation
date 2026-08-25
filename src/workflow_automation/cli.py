"""Command-line interface for the workflow bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "repositories.json"
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspaces"
DEFAULT_CONTROLLER_ENV = PROJECT_ROOT / ".venv"


class BootstrapError(RuntimeError):
    """Raised when repository state is unsafe or invalid."""


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    revision: str
    commit: str
    directory: str
    environment_file: str | None = None
    install_extras: str | None = None
    import_name: str | None = None
    validation_imports: tuple[str, ...] = ()


def path_summary(path: Path) -> dict[str, object]:
    """Describe a path using metadata-only filesystem operations."""
    expanded = path.expanduser()
    return {
        "path": str(expanded),
        "exists": expanded.exists(),
        "is_directory": expanded.is_dir(),
        "readable": os.access(expanded, os.R_OK) if expanded.exists() else False,
        "writable": os.access(expanded, os.W_OK) if expanded.exists() else False,
    }


def repository_summary(repository: Repository, workspace: Path) -> dict[str, object]:
    """Inspect a configured checkout without fetching or changing it."""
    destination = workspace / repository.directory
    summary: dict[str, object] = {
        "name": repository.name,
        "path": str(destination),
        "configured_revision": repository.revision,
        "configured_commit": repository.commit,
        "exists": destination.exists(),
        "git_checkout": (destination / ".git").exists(),
    }
    if not summary["git_checkout"]:
        return summary
    try:
        summary.update(
            {
                "commit": run_git(["rev-parse", "--verify", "HEAD"], cwd=destination),
                "branch": run_git(["branch", "--show-current"], cwd=destination) or None,
                "origin_matches": normalize_git_url(
                    run_git(["remote", "get-url", "origin"], cwd=destination)
                )
                == normalize_git_url(repository.url),
                "local_changes": bool(run_git(["status", "--porcelain"], cwd=destination)),
            }
        )
        summary["revision_matches"] = (
            summary["branch"] == repository.revision
            and summary["commit"] == repository.commit
        )
    except BootstrapError as exc:
        summary["inspection_error"] = str(exc)
    return summary


def collect_diagnostics(config_path: Path, workspace: Path) -> dict[str, object]:
    """Collect local environment facts using read-only operations only."""
    tool_names = (
        "git",
        "python3",
        "law",
        "mamba",
        "conda",
        "condor_q",
        "condor_submit",
        "voms-proxy-info",
    )
    environment = {
        name: {"set": name in os.environ}
        for name in ("BATCH_SYSTEM", "CONDOR_CONFIG", "X509_USER_PROXY", "VIRTUAL_ENV")
    }
    proxy = os.environ.get("X509_USER_PROXY")
    if proxy:
        proxy_summary = path_summary(Path(proxy))
        proxy_summary.pop("path")
        environment["X509_USER_PROXY"].update(proxy_summary)

    result: dict[str, object] = {
        "schema_version": 1,
        "read_only": True,
        "system": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
        },
        "tools": {name: shutil.which(name) for name in tool_names},
        "environment": environment,
        "config": path_summary(config_path),
        "workspace": path_summary(workspace),
        "repositories": [],
    }
    try:
        repositories = load_repositories(config_path)
    except BootstrapError as exc:
        result["configuration_error"] = str(exc)
    else:
        result["repositories"] = [
            repository_summary(repository, workspace) for repository in repositories
        ]
    return result


def print_diagnostics(diagnostics: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
        return

    system = diagnostics["system"]
    assert isinstance(system, dict)
    print("WorkflowAutomation environment diagnostics (read-only)")
    print(f"system: {system['hostname']} | {system['platform']}")
    print(f"python: {system['python']} ({system['python_executable']})")
    tools = diagnostics["tools"]
    assert isinstance(tools, dict)
    for name, location in tools.items():
        print(f"tool {name}: {location or 'not found'}")
    environment = diagnostics["environment"]
    assert isinstance(environment, dict)
    for name, detail in environment.items():
        assert isinstance(detail, dict)
        suffix = ""
        if "exists" in detail:
            suffix = f", target exists={detail['exists']}, readable={detail['readable']}"
        print(f"environment {name}: set={detail['set']}{suffix}")
    config = diagnostics["config"]
    assert isinstance(config, dict)
    print(f"config: {config['path']} (exists={config['exists']}, readable={config['readable']})")
    workspace = diagnostics["workspace"]
    assert isinstance(workspace, dict)
    print(
        f"workspace: {workspace['path']} "
        f"(exists={workspace['exists']}, readable={workspace['readable']}, "
        f"writable={workspace['writable']})"
    )
    repositories = diagnostics["repositories"]
    assert isinstance(repositories, list)
    for repository in repositories:
        assert isinstance(repository, dict)
        state = "absent"
        detail = ""
        if repository["git_checkout"]:
            state = str(repository.get("commit", "invalid checkout"))[:12]
            detail = (
                f", branch={repository.get('branch') or '<detached>'}"
                f", origin_matches={repository.get('origin_matches', False)}"
                f", revision_matches={repository.get('revision_matches', False)}"
                f", local_changes={repository.get('local_changes', False)}"
            )
        elif repository["exists"]:
            state = "present, not a git checkout"
        print(f"repository {repository['name']}: {state}{detail} ({repository['path']})")
    if "configuration_error" in diagnostics:
        print(f"configuration: {diagnostics['configuration_error']}")


def run_git(arguments: Sequence[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise BootstrapError("git is required but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise BootstrapError(detail or f"git {' '.join(arguments)} failed") from exc
    return result.stdout.strip()


def run_program(arguments: Sequence[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise BootstrapError(f"required executable was not found: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise BootstrapError(detail or f"{' '.join(arguments)} failed") from exc
    return result.stdout.strip()


def normalize_git_url(url: str) -> str:
    normalized = url.rstrip("/")
    return normalized[:-4] if normalized.endswith(".git") else normalized


def load_repositories(config_path: Path) -> list[Repository]:
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise BootstrapError(f"configuration not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"invalid JSON in {config_path}: {exc}") from exc

    repositories = []
    for name, values in data.get("repositories", {}).items():
        try:
            repositories.append(
                Repository(
                    name=name,
                    url=values["url"],
                    revision=values["revision"],
                    commit=values["commit"],
                    directory=values.get("directory", name),
                    environment_file=values.get("environment_file"),
                    install_extras=values.get("install_extras"),
                    import_name=values.get("import_name"),
                    validation_imports=tuple(values.get("validation_imports", ())),
                )
            )
        except KeyError as exc:
            raise BootstrapError(f"repository {name!r} is missing {exc.args[0]!r}") from exc
    if not repositories:
        raise BootstrapError(f"no repositories configured in {config_path}")
    return repositories


def validate_existing(repository: Repository, destination: Path) -> str:
    if not (destination / ".git").exists():
        raise BootstrapError(
            f"{destination} exists but is not a git checkout; move it aside or choose another workspace"
        )

    try:
        commit = run_git(["rev-parse", "--verify", "HEAD"], cwd=destination)
    except BootstrapError as exc:
        raise BootstrapError(
            f"{destination} is an incomplete checkout with no valid HEAD; move it aside and rerun bootstrap"
        ) from exc

    origin = run_git(["remote", "get-url", "origin"], cwd=destination)
    if normalize_git_url(origin) != normalize_git_url(repository.url):
        raise BootstrapError(
            f"{destination} has unexpected origin {origin!r}; expected {repository.url!r}"
        )
    return commit


def repository_is_current(repository: Repository, destination: Path) -> bool:
    """Check configured checkout identity without fetching or changing it."""
    try:
        commit = validate_existing(repository, destination)
        branch = run_git(["branch", "--show-current"], cwd=destination)
        dirty = bool(run_git(["status", "--porcelain"], cwd=destination))
    except BootstrapError:
        return False
    return not dirty and branch == repository.revision and commit == repository.commit


def update_existing(repository: Repository, destination: Path) -> str:
    """Fast-forward a clean configured branch to its pinned commit."""
    commit = validate_existing(repository, destination)
    branch = run_git(["branch", "--show-current"], cwd=destination)
    dirty = bool(run_git(["status", "--porcelain"], cwd=destination))
    if dirty:
        raise BootstrapError(
            f"{destination} has local changes; commit or move them aside before updating"
        )
    if branch != repository.revision:
        raise BootstrapError(
            f"{destination} is on branch {branch or '<detached>'!r}; expected "
            f"{repository.revision!r}. Switch branches explicitly before rerunning setup"
        )
    if commit == repository.commit:
        print(f"[ready] {repository.name}: {destination} ({commit[:12]}, clean)")
        return commit

    print(
        f"[update] {repository.name}: {commit[:12]} -> {repository.commit[:12]} "
        f"on {repository.revision}"
    )
    run_git(["fetch", "origin", repository.revision], cwd=destination)
    try:
        pinned = run_git(
            ["rev-parse", "--verify", f"{repository.commit}^{{commit}}"], cwd=destination
        )
        run_git(["merge-base", "--is-ancestor", pinned, "FETCH_HEAD"], cwd=destination)
    except BootstrapError as exc:
        raise BootstrapError(
            f"configured commit {repository.commit!r} is not on "
            f"origin/{repository.revision}; update repositories.json deliberately"
        ) from exc
    try:
        run_git(["merge-base", "--is-ancestor", "HEAD", pinned], cwd=destination)
    except BootstrapError as exc:
        raise BootstrapError(
            f"{destination} cannot be fast-forwarded from {commit[:12]} to {pinned[:12]}; "
            "inspect the branch manually"
        ) from exc
    run_git(["merge", "--ff-only", pinned], cwd=destination)
    updated = run_git(["rev-parse", "--verify", "HEAD"], cwd=destination)
    print(f"[ready] {repository.name}: {destination} ({updated[:12]}, clean)")
    return updated


def prepare_repository(repository: Repository, workspace: Path) -> str:
    destination = workspace / repository.directory
    if destination.exists():
        return update_existing(repository, destination)

    workspace.mkdir(parents=True, exist_ok=True)
    print(f"[clone] {repository.name}: {repository.url} -> {destination}")
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{repository.directory}.clone-", dir=workspace)
    )
    try:
        run_git(
            [
                "clone",
                "--branch",
                repository.revision,
                "--single-branch",
                repository.url,
                str(temporary_directory),
            ]
        )
        fetched = run_git(["rev-parse", "--verify", "HEAD"], cwd=temporary_directory)
        try:
            pinned = run_git(
                ["rev-parse", "--verify", f"{repository.commit}^{{commit}}"],
                cwd=temporary_directory,
            )
            run_git(["merge-base", "--is-ancestor", pinned, fetched], cwd=temporary_directory)
        except BootstrapError as exc:
            raise BootstrapError(
                f"configured commit {repository.commit!r} is not on "
                f"origin/{repository.revision}"
            ) from exc
        if fetched != pinned:
            run_git(["checkout", "--detach", pinned], cwd=temporary_directory)
            run_git(["branch", "--force", repository.revision, pinned], cwd=temporary_directory)
            run_git(["checkout", repository.revision], cwd=temporary_directory)
        validate_existing(repository, temporary_directory)
        os.replace(temporary_directory, destination)
    finally:
        if temporary_directory.exists():
            shutil.rmtree(temporary_directory)
    return validate_existing(repository, destination)


def bootstrap(config_path: Path, workspace: Path, selected: Sequence[str]) -> None:
    repositories = load_repositories(config_path)
    by_name = {repository.name: repository for repository in repositories}
    names = list(selected) if selected else list(by_name)
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise BootstrapError(f"unknown repositories: {', '.join(unknown)}")

    for name in names:
        prepare_repository(by_name[name], workspace.resolve())


def environment_python(prefix: Path) -> Path:
    return prefix / ("python.exe" if os.name == "nt" else "bin/python")


def validate_environment(repository: Repository, checkout: Path, prefix: Path) -> bool:
    python = environment_python(prefix)
    if not python.is_file() or not repository.import_name:
        return False
    try:
        module_path = run_program(
            [
                str(python),
                "-c",
                f"import {repository.import_name}; print({repository.import_name}.__file__)",
            ]
        )
    except BootstrapError:
        return False
    try:
        Path(module_path).resolve().relative_to(checkout.resolve())
    except (OSError, ValueError):
        return False
    for import_name in repository.validation_imports:
        try:
            run_program([str(python), "-c", f"import {import_name}"])
        except BootstrapError:
            return False
    return True


def prepare_environment(repository: Repository, workspace: Path, environment_root: Path) -> None:
    checkout = workspace / repository.directory
    prefix = environment_root / repository.directory
    if validate_environment(repository, checkout, prefix):
        print(f"[ready] {repository.name} environment: {prefix}")
        return

    python = environment_python(prefix)
    if prefix.exists() and not python.is_file():
        raise BootstrapError(
            f"{prefix} exists but is not a usable environment; inspect and move it aside before "
            "rerunning setup"
        )

    if not repository.environment_file:
        raise BootstrapError(f"repository {repository.name!r} has no environment_file configured")
    if not repository.import_name:
        raise BootstrapError(f"repository {repository.name!r} has no import_name configured")
    definition = checkout / repository.environment_file
    if not definition.is_file():
        raise BootstrapError(f"environment definition not found: {definition}")

    if not python.is_file():
        creator = shutil.which("mamba") or shutil.which("conda")
        if not creator:
            raise BootstrapError(
                f"mamba or conda is required to create the {repository.name} environment"
            )
        environment_root.mkdir(parents=True, exist_ok=True)
        print(f"[create] {repository.name} environment: {definition} -> {prefix}")
        try:
            run_program(
                [
                    creator,
                    "env",
                    "create",
                    "--yes",
                    "--prefix",
                    str(prefix),
                    "--file",
                    str(definition),
                ]
            )
        except BootstrapError as exc:
            raise BootstrapError(
                f"environment creation failed; a partial prefix may remain at {prefix}. "
                "Inspect and move it aside before retrying. Details: {exc}"
            ) from exc

    install_target = "."
    if repository.install_extras:
        install_target += f"[{repository.install_extras}]"
    print(f"[install] {repository.name}: editable {install_target}")
    run_program([str(python), "-m", "pip", "install", "--editable", install_target], cwd=checkout)
    if not validate_environment(repository, checkout, prefix):
        raise BootstrapError(
            f"environment validation failed: {prefix} cannot import "
            f"{repository.import_name} after installation"
        )
    print(f"[ready] {repository.name} environment: {prefix}")


def prepare_controller(prefix: Path) -> None:
    """Create the project-local environment used to run law tasks."""
    resolved_prefix = prefix.expanduser().resolve()
    python = environment_python(resolved_prefix)
    if python.is_file():
        try:
            run_program([str(python), "-c", "import law, workflow_automation"])
        except BootstrapError:
            pass
        else:
            print(f"[ready] WorkflowAutomation controller: {resolved_prefix}")
            return
    elif resolved_prefix.exists():
        raise BootstrapError(
            f"{resolved_prefix} exists but is not a usable Python environment; inspect and move "
            "it aside before rerunning setup-controller"
        )
    else:
        print(f"[create] WorkflowAutomation controller: {resolved_prefix}")
        try:
            venv.EnvBuilder(with_pip=True).create(resolved_prefix)
        except Exception as exc:
            raise BootstrapError(
                f"controller creation failed; a partial environment may remain at "
                f"{resolved_prefix}: {exc}"
            ) from exc

    print("[install] WorkflowAutomation and law")
    run_program([str(python), "-m", "pip", "install", "--editable", str(PROJECT_ROOT)])
    try:
        run_program([str(python), "-c", "import law, workflow_automation"])
    except BootstrapError as exc:
        raise BootstrapError(f"controller validation failed: {exc}") from exc
    print(f"[ready] WorkflowAutomation controller: {resolved_prefix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="clone missing repositories and validate existing checkouts"
    )
    bootstrap_parser.add_argument("repositories", nargs="*", help="repository names (default: all)")
    bootstrap_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    bootstrap_parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    controller_parser = subparsers.add_parser(
        "setup-controller", help="create the project-local environment containing law"
    )
    controller_parser.add_argument(
        "--prefix",
        type=Path,
        default=DEFAULT_CONTROLLER_ENV,
        help="controller environment path (default: PROJECT/.venv)",
    )
    diagnose_parser = subparsers.add_parser(
        "diagnose", help="report read-only local and cluster environment diagnostics"
    )
    diagnose_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    diagnose_parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    diagnose_parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            bootstrap(args.config, args.workspace, args.repositories)
        elif args.command == "setup-controller":
            prepare_controller(args.prefix)
        elif args.command == "diagnose":
            print_diagnostics(
                collect_diagnostics(args.config, args.workspace.resolve()), args.format
            )
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
