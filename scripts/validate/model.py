"""Shared result dataclasses for the harness.

Kept in their own module so :mod:`predicates`, :mod:`runner`, and :mod:`report`
can share them without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Terminal status of a scenario."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


@dataclass(frozen=True)
class RunResult:
    """Captured outcome of one binary invocation.

    Attributes:
        argv: The full argument vector that was executed.
        returncode: Process exit code (``-1`` sentinel if it never started).
        stdout: Captured standard output.
        stderr: Captured standard error.
        duration_s: Wall-clock seconds the process ran.
        timed_out: True if the process was killed for exceeding its deadline.
    """

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


@dataclass(frozen=True)
class AssertionOutcome:
    """Result of evaluating one predicate against a :class:`RunResult`.

    Attributes:
        description: Human-readable statement of what was asserted.
        passed: Whether the assertion held.
        detail: Short evidence string (the actual value seen) for the report.
    """

    description: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    """Aggregated outcome of one scenario across all its steps.

    Attributes:
        name: Scenario name (matches the registry entry).
        needs: Resource lane the scenario ran in (``none``/``shared-vm``/``exclusive-vm``).
        tags: Issue references / labels carried from the registry.
        status: Terminal :class:`Status`.
        assertions: Every assertion evaluated, in order.
        observations: Soft, non-failing notes (e.g. the #148 DHCPv6 ``/128``).
        runs: The captured :class:`RunResult` of each step.
        error: Populated when ``status`` is :attr:`Status.ERROR`.
        duration_s: Total wall-clock seconds for the scenario.
    """

    name: str
    needs: str
    tags: list[str] = field(default_factory=list)
    status: Status = Status.PASS
    assertions: list[AssertionOutcome] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    runs: list[RunResult] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    def record(self, outcome: AssertionOutcome) -> None:
        """Append an assertion and downgrade status to FAIL if it failed.

        Args:
            outcome: The evaluated assertion to record.
        """
        self.assertions.append(outcome)
        if not outcome.passed and self.status is Status.PASS:
            self.status = Status.FAIL
