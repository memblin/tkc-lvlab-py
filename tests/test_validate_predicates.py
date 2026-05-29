"""Unit tests for the harness's assertion predicates.

Each predicate is the thing that turns raw process output into a pass/fail the
report projects, so a bug here means a green report that proved nothing.
"""

from __future__ import annotations

from validate import predicates as P
from validate.model import RunResult


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> RunResult:
    return RunResult(
        argv=["lvlab"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_s=0.0,
    )


def test_exit_code_pass_and_fail() -> None:
    assert P.ExitCode(0).check(_result(returncode=0)).passed
    bad = P.ExitCode(0).check(_result(returncode=2))
    assert not bad.passed
    assert "got 2" in bad.detail


def test_stdout_contains_is_case_sensitive() -> None:
    assert P.StdoutContains("Usage").check(_result(stdout="Usage: lvlab")).passed
    assert not P.StdoutContains("Usage").check(_result(stdout="usage: lvlab")).passed


def test_output_contains_checks_both_streams() -> None:
    """Boxed errors land on stderr; OutputContains must see either stream."""
    assert P.OutputContains("Error").check(_result(stderr="╭─ Error ─╮")).passed
    assert P.OutputContains("Error").check(_result(stdout="Error")).passed
    assert (
        not P.OutputContains("Error").check(_result(stdout="fine", stderr="ok")).passed
    )


def test_output_not_contains() -> None:
    assert P.OutputNotContains("Traceback").check(_result(stdout="clean")).passed
    assert (
        not P.OutputNotContains("Traceback")
        .check(_result(stderr="Traceback (most recent"))
        .passed
    )


def test_output_matches_regex_per_stream() -> None:
    assert P.OutputMatches(r"\d+\.\d+\.\d+").check(_result(stdout="lvlab 0.6.0")).passed
    assert (
        P.OutputMatches(r"not found", in_stderr=True)
        .check(_result(stderr="network not found"))
        .passed
    )
    assert (
        not P.OutputMatches(r"\d+\.\d+\.\d+")
        .check(_result(stdout="no version here"))
        .passed
    )
