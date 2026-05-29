"""Composable assertions evaluated against a :class:`~validate.model.RunResult`.

Each predicate is a small frozen dataclass with a :meth:`check` method, so a
registry entry's assertion list is itself declarative data — the report can
render each predicate's ``describe()`` whether or not it passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from validate.model import AssertionOutcome, RunResult


def _clip(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate ``text`` for a one-line report detail."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass(frozen=True)
class ExitCode:
    """Assert the process exit code equals ``expected``."""

    expected: int

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``.

        Args:
            result: The captured invocation.

        Returns:
            The :class:`AssertionOutcome`.
        """
        ok = result.returncode == self.expected
        return AssertionOutcome(
            description=f"exit code == {self.expected}",
            passed=ok,
            detail=f"got {result.returncode}",
        )


@dataclass(frozen=True)
class StdoutContains:
    """Assert ``needle`` appears in stdout (case-sensitive substring)."""

    needle: str

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``."""
        ok = self.needle in result.stdout
        return AssertionOutcome(
            description=f"stdout contains {self.needle!r}",
            passed=ok,
            detail="" if ok else f"stdout was {_clip(result.stdout)!r}",
        )


@dataclass(frozen=True)
class StderrContains:
    """Assert ``needle`` appears in stderr (case-sensitive substring)."""

    needle: str

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``."""
        ok = self.needle in result.stderr
        return AssertionOutcome(
            description=f"stderr contains {self.needle!r}",
            passed=ok,
            detail="" if ok else f"stderr was {_clip(result.stderr)!r}",
        )


@dataclass(frozen=True)
class OutputContains:
    """Assert ``needle`` appears in stdout **or** stderr.

    Useful for messages whose stream placement we don't want to over-specify
    (Rich/Click route some boxed output to stderr).
    """

    needle: str

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``."""
        ok = self.needle in result.stdout or self.needle in result.stderr
        return AssertionOutcome(
            description=f"stdout/stderr contains {self.needle!r}",
            passed=ok,
            detail=(
                ""
                if ok
                else f"out={_clip(result.stdout)!r} err={_clip(result.stderr)!r}"
            ),
        )


@dataclass(frozen=True)
class OutputNotContains:
    """Assert ``needle`` appears in neither stdout nor stderr."""

    needle: str

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``."""
        ok = self.needle not in result.stdout and self.needle not in result.stderr
        return AssertionOutcome(
            description=f"output does not contain {self.needle!r}",
            passed=ok,
            detail="" if ok else "needle was present",
        )


@dataclass(frozen=True)
class OutputMatches:
    """Assert a regex matches stdout (or stderr when ``in_stderr``)."""

    pattern: str
    in_stderr: bool = False

    def check(self, result: RunResult) -> AssertionOutcome:
        """Evaluate against ``result``."""
        haystack = result.stderr if self.in_stderr else result.stdout
        ok = re.search(self.pattern, haystack) is not None
        stream = "stderr" if self.in_stderr else "stdout"
        return AssertionOutcome(
            description=f"{stream} matches /{self.pattern}/",
            passed=ok,
            detail="" if ok else f"{stream} was {_clip(haystack)!r}",
        )
