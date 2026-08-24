"""Command-line interface for the workflow bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "repositories.json"
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspaces"


class BootstrapError(RuntimeError):
    """Raised when repository state is unsafe or invalid."""


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    revision: str
    directory: str


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
                    directory=values.get("directory", name),
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
    dirty = bool(run_git(["status", "--porcelain"], cwd=destination))
    state = "with local changes" if dirty else "clean"
    print(f"[ready] {repository.name}: {destination} ({commit[:12]}, {state})")
    return commit


def prepare_repository(repository: Repository, workspace: Path) -> str:
    destination = workspace / repository.directory
    if destination.exists():
        return validate_existing(repository, destination)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="clone missing repositories and validate existing checkouts"
    )
    bootstrap_parser.add_argument("repositories", nargs="*", help="repository names (default: all)")
    bootstrap_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    bootstrap_parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            bootstrap(args.config, args.workspace, args.repositories)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
