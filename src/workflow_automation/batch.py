"""Site-aware batch resource resolution and job failure classification.

Nothing here is specific to HiggsDNA or to effective events. Every stage of the
analysis submits Condor jobs, so how a failed job is diagnosed and what it should
be given on a retry belongs in one place.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from workflow_automation.cli import BootstrapError


# Why a job did not finish. The distinction matters because only some of these
# can be fixed by resubmitting, and resubmitting the rest wastes the farm.
WALLTIME = "walltime"
MEMORY = "memory"
INFRASTRUCTURE = "infrastructure"
APPLICATION = "application"
INCOMPLETE = "incomplete"
UNKNOWN = "unknown"

# Causes worth resubmitting, and how. An application error is absent on purpose:
# the code or configuration is wrong, so a retry produces the same failure.
RETRYABLE = {WALLTIME, MEMORY, INFRASTRUCTURE, INCOMPLETE}


@dataclass(frozen=True)
class NodeClass:
    name: str
    max_runtime_seconds: int
    memory_per_slot_mb: int


@dataclass(frozen=True)
class Resources:
    request_cpus: int
    request_memory_mb: int
    runtime_seconds: int

    def submit_lines(self, runtime_attribute: str) -> list[str]:
        return [
            f"request_cpus = {self.request_cpus}",
            f"request_memory = {self.request_memory_mb}",
            f"{runtime_attribute} = {self.runtime_seconds}",
        ]


@dataclass(frozen=True)
class Site:
    name: str
    runtime_attribute: str
    node_classes: tuple[NodeClass, ...]
    max_cpus_per_job: int
    runtime_ladder: tuple[int, ...]
    memory_ladder: tuple[int, ...]
    max_attempts: int

    def classes_for(self, runtime_seconds: int) -> list[NodeClass]:
        return [item for item in self.node_classes if item.max_runtime_seconds >= runtime_seconds]

    def resolve(self, runtime_seconds: int, memory_mb: int) -> Resources:
        """Turn a wall time and a memory need into a schedulable request.

        Cores are derived from the *largest* per-slot memory among the classes
        that can host this wall time, because extra cores are a cost to be
        avoided: a single-threaded job given two slots wastes one.

        This is also where wall time and memory stop being independent. A three
        hour job can land on short-highmem and get 12GB on one core, but asking
        for ten hours restricts it to medium and long, where a slot is 4GB, so
        the same memory now costs more cores.
        """
        eligible = self.classes_for(runtime_seconds)
        if not eligible:
            longest = max(item.max_runtime_seconds for item in self.node_classes)
            raise BootstrapError(
                f"{runtime_seconds}s exceeds the longest node class at site {self.name!r} "
                f"({longest}s); no resubmission can succeed and the work needs splitting"
            )
        per_slot = max(item.memory_per_slot_mb for item in eligible)
        cpus = max(1, math.ceil(memory_mb / per_slot))
        if cpus > self.max_cpus_per_job:
            raise BootstrapError(
                f"{memory_mb}MB within {runtime_seconds}s needs {cpus} cores at site "
                f"{self.name!r}, above the configured maximum of {self.max_cpus_per_job}. "
                "This is a deliberate stop rather than a farm limit: needing more cores "
                "means the memory per job is too high, and the work should be split "
                "differently instead. Raise max_cpus_per_job in the batch configuration "
                "if you would rather trade that away."
            )
        return Resources(
            request_cpus=cpus, request_memory_mb=memory_mb, runtime_seconds=runtime_seconds
        )

    def next_step(self, cause: str, current: Resources | None) -> Resources:
        """Advance only the axis that caused the failure.

        Wall time and memory have separate ladders on purpose. Giving a job that
        ran out of time more memory, or vice versa, produces a retry that fails
        in exactly the same way while costing more of the farm. Holding the other
        axis steady also keeps the core count as low as the site allows.
        """
        if cause not in RETRYABLE:
            raise BootstrapError(f"failure cause {cause!r} is not resubmittable")
        if current is None:
            return self.resolve(self.runtime_ladder[0], self.memory_ladder[0])

        if cause == WALLTIME:
            longer = [item for item in self.runtime_ladder if item > current.runtime_seconds]
            if not longer:
                raise BootstrapError(
                    f"no configured wall time beyond {current.runtime_seconds}s at site "
                    f"{self.name!r}; the work needs splitting rather than resubmitting"
                )
            return self.resolve(longer[0], current.request_memory_mb)

        if cause == MEMORY:
            larger = [item for item in self.memory_ladder if item > current.request_memory_mb]
            if not larger:
                raise BootstrapError(
                    f"no configured memory beyond {current.request_memory_mb}MB at site "
                    f"{self.name!r}; the job needs investigating rather than resubmitting"
                )
            return self.resolve(current.runtime_seconds, larger[0])

        # Nothing about the job was wrong, so retry it exactly as it was.
        return current


def load_site(config_path: Path, name: str) -> Site:
    try:
        data = json.loads(Path(config_path).read_text())
    except FileNotFoundError as exc:
        raise BootstrapError(f"batch configuration not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"invalid JSON in {config_path}: {exc}") from exc
    try:
        site = data["sites"][name]
    except KeyError as exc:
        known = ", ".join(sorted(data.get("sites", {})))
        raise BootstrapError(f"unknown batch site {name!r}; configured: {known}") from exc
    classes = tuple(
        NodeClass(
            name=item["name"],
            max_runtime_seconds=int(item["max_runtime_seconds"]),
            memory_per_slot_mb=int(item["memory_per_slot_mb"]),
        )
        for item in site["node_classes"]
    )
    if not classes:
        raise BootstrapError(f"batch site {name!r} declares no node classes")
    escalation = site.get("escalation", {})
    runtime_ladder = tuple(int(item) for item in escalation.get("runtime_seconds", ()))
    memory_ladder = tuple(int(item) for item in escalation.get("memory_mb", ()))
    if not runtime_ladder or not memory_ladder:
        raise BootstrapError(
            f"batch site {name!r} must declare escalation.runtime_seconds and escalation.memory_mb"
        )
    return Site(
        name=name,
        runtime_attribute=site.get("runtime_attribute", "+MaxRuntime"),
        node_classes=classes,
        max_cpus_per_job=int(site.get("max_cpus_per_job", 1)),
        runtime_ladder=tuple(sorted(runtime_ladder)),
        memory_ladder=tuple(sorted(memory_ladder)),
        max_attempts=int(escalation.get("max_attempts", 3)),
    )


# Condor states its own reasons in the job log; the application states its in stderr.
_WALLTIME_PATTERNS = (
    re.compile(r"MaxRuntime", re.IGNORECASE),
    re.compile(r"max(imum)?\s+(wall\s*)?(clock|runtime|time)\s+exceeded", re.IGNORECASE),
    re.compile(r"job\s+ran\s+for\s+too\s+long", re.IGNORECASE),
)
_MEMORY_PATTERNS = (
    re.compile(r"memory\s+usage\s+exceeded", re.IGNORECASE),
    re.compile(r"exceeded\s+memory", re.IGNORECASE),
    re.compile(r"out\s+of\s+memory", re.IGNORECASE),
    re.compile(r"\bOOM\b"),
)
_APPLICATION_PATTERNS = (
    re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE),
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ImportError"),
    re.compile(r"SyntaxError"),
)
_INFRASTRUCTURE_PATTERNS = (
    re.compile(r"was evicted", re.IGNORECASE),
    re.compile(r"shadow exception", re.IGNORECASE),
    re.compile(r"failed to execute", re.IGNORECASE),
    re.compile(r"transfer\s+.*fail", re.IGNORECASE),
)


def classify_failure(log_text: str = "", stderr_text: str = "", stdout_text: str = "") -> str:
    """Say why a job did not finish, from what Condor and the job itself recorded.

    Resource limits are checked before application errors: a job killed for
    exceeding its memory often dies mid-traceback, and reading that traceback as
    a code fault would stop us retrying something a larger slot would complete.
    """
    condor = log_text or ""
    if any(pattern.search(condor) for pattern in _MEMORY_PATTERNS):
        return MEMORY
    if any(pattern.search(condor) for pattern in _WALLTIME_PATTERNS):
        return WALLTIME
    combined = f"{stderr_text}\n{stdout_text}"
    if any(pattern.search(combined) for pattern in _MEMORY_PATTERNS):
        return MEMORY
    if any(pattern.search(combined) for pattern in _APPLICATION_PATTERNS):
        return APPLICATION
    if any(pattern.search(condor) for pattern in _INFRASTRUCTURE_PATTERNS):
        return INFRASTRUCTURE
    if condor.strip() or combined.strip():
        # It started and produced something, but never reached its completion
        # marker and gave no reason. Retrying once is reasonable; the attempt
        # cap is what stops this becoming a loop.
        return INCOMPLETE
    return UNKNOWN
