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
class Slot:
    """One concrete way to run a job: a node class and a number of cores."""

    node_class: NodeClass
    cpus: int

    @property
    def runtime_seconds(self) -> int:
        # Stay a second inside the class limit, as the existing submit files do.
        return self.node_class.max_runtime_seconds - 1

    @property
    def memory_mb(self) -> int:
        return self.node_class.memory_per_slot_mb * self.cpus

    @property
    def cost(self) -> tuple[int, int, int]:
        """Cheapest first: fewest cores, then shortest class, then smallest slot.

        Cores lead because a single-threaded job handed two slots wastes one.
        Class length comes next so a job that needs ten hours is not sent to the
        forty-eight hour queue for no reason.
        """
        return (self.cpus, self.node_class.max_runtime_seconds, self.node_class.memory_per_slot_mb)

    def submit_lines(self, runtime_attribute: str) -> list[str]:
        return [
            f"request_cpus = {self.cpus}",
            f"request_memory = {self.memory_mb}",
            f"{runtime_attribute} = {self.runtime_seconds}",
        ]

    def describe(self) -> str:
        return (
            f"{self.node_class.name} x{self.cpus} "
            f"({self.runtime_seconds / 3600:.0f}h, {self.memory_mb}MB)"
        )


@dataclass(frozen=True)
class Demand:
    """What a job has been *shown* to need, from the failures actually seen.

    Only observed failures raise these floors. A job that ran out of time has
    demonstrated nothing about its memory, so escalating its wall clock must not
    drag along whatever memory its previous slot happened to provide.
    """

    minimum_runtime_seconds: int = 0
    minimum_memory_mb: int = 0

    def after(self, cause: str, slot: "Slot") -> "Demand":
        if cause == WALLTIME:
            return Demand(max(self.minimum_runtime_seconds, slot.runtime_seconds + 1),
                          self.minimum_memory_mb)
        if cause == MEMORY:
            return Demand(self.minimum_runtime_seconds,
                          max(self.minimum_memory_mb, slot.memory_mb + 1))
        return self


@dataclass(frozen=True)
class Site:
    name: str
    runtime_attribute: str
    node_classes: tuple[NodeClass, ...]
    max_cpus_per_job: int
    max_attempts: int

    def slots(self) -> list[Slot]:
        options = [
            Slot(node_class=item, cpus=cpus)
            for item in self.node_classes
            for cpus in range(1, self.max_cpus_per_job + 1)
        ]
        return sorted(options, key=lambda item: item.cost)

    def classes_for(self, runtime_seconds: int) -> list[NodeClass]:
        return [item for item in self.node_classes if item.max_runtime_seconds >= runtime_seconds]

    def select(self, demand: Demand) -> Slot:
        """Cheapest slot that satisfies everything the job has been shown to need."""
        for slot in self.slots():
            if (
                slot.runtime_seconds >= demand.minimum_runtime_seconds
                and slot.memory_mb >= demand.minimum_memory_mb
            ):
                return slot
        raise BootstrapError(
            f"no slot at site {self.name!r} provides at least "
            f"{demand.minimum_runtime_seconds}s and {demand.minimum_memory_mb}MB within "
            f"{self.max_cpus_per_job} core(s). This is a deliberate stop rather than a farm "
            "limit: the work should be split differently instead. Raise max_cpus_per_job in "
            "the batch configuration if you would rather trade that away."
        )

    def next_step(self, cause: str, slot: Slot | None, demand: Demand | None = None) -> tuple[Slot, Demand]:
        """Escalate only what the observed failure actually demonstrated."""
        if cause not in RETRYABLE:
            raise BootstrapError(f"failure cause {cause!r} is not resubmittable")
        current = demand or Demand()
        if slot is None:
            chosen = self.select(current)
            return chosen, current
        updated = current.after(cause, slot)
        return self.select(updated), updated


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
    return Site(
        name=name,
        runtime_attribute=site.get("runtime_attribute", "+MaxRuntime"),
        node_classes=classes,
        max_cpus_per_job=int(site.get("max_cpus_per_job", 1)),
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
