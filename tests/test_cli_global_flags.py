"""Unit tests for global flags accepted in either position (issue #133).

The root-owned globals — ``--no-color``, ``-v``/``--verbose``,
``-q``/``--quiet`` — must work the same before *or* after the subcommand:
``lvlab --no-color smoke`` and ``lvlab smoke --no-color`` are equivalent.
:class:`tkc_lvlab.cli.GlobalFlagGroup` hoists those globals out of ``argv``
before dispatch so they reach the root callback regardless of position, while
an unknown flag after the subcommand still errors (the hoist is scoped to the
known globals, not a blanket "accept anything anywhere").
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import _is_global_flag, app
from tkc_lvlab.utils import output

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_color_state():
    """Isolate colour state — the ``--no-color`` callback flips a module global
    *and* sets ``NO_COLOR`` in the environment, which would otherwise leak
    between tests."""
    prev = os.environ.get("NO_COLOR")
    output.set_no_color(False)
    os.environ.pop("NO_COLOR", None)
    yield
    output.set_no_color(False)
    if prev is None:
        os.environ.pop("NO_COLOR", None)
    else:
        os.environ["NO_COLOR"] = prev


# ---------------------------------------------------------------------------
# The pure hoist predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["--no-color", "--verbose", "--quiet", "-v", "-q", "-vv", "-qq", "-vq", "-qv"],
)
def test_is_global_flag_matches_long_and_short_clusters(token):
    assert _is_global_flag(token) is True


@pytest.mark.parametrize(
    "token",
    # a non-global long flag, command short flags, the subcommand name, a
    # positional, the end-of-options marker, a mixed cluster, a value that
    # merely starts like a short flag, a near-miss long flag, and empty.
    [
        "--config",
        "-c",
        "-f",
        "-y",
        "smoke",
        "vm01",
        "--",
        "-vx",
        "-q-thing",
        "--no-colors",
        "",
    ],
)
def test_is_global_flag_rejects_non_globals(token):
    assert _is_global_flag(token) is False


# ---------------------------------------------------------------------------
# Either position, exercised against the real app
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--no-color", "status"], ["status", "--no-color"]])
def test_no_color_accepted_before_or_after_subcommand(argv):
    with (
        mock.patch.object(cli, "configure_logging"),
        mock.patch.object(cli, "parse_config", return_value=None),
    ):
        runner.invoke(app, argv)
    assert output.color_disabled() is True


def test_no_color_absent_leaves_color_enabled():
    with (
        mock.patch.object(cli, "configure_logging"),
        mock.patch.object(cli, "parse_config", return_value=None),
    ):
        runner.invoke(app, ["status"])
    assert output.color_disabled() is False


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["-vv", "status"], 2),
        (["status", "-vv"], 2),
        (["status", "-v"], 1),
        (["--verbose", "status"], 1),
    ],
)
def test_verbose_count_accepted_before_or_after_subcommand(argv, expected):
    with (
        mock.patch.object(cli, "configure_logging") as cfg,
        mock.patch.object(cli, "parse_config", return_value=None),
    ):
        runner.invoke(app, argv)
    assert cfg.call_args.kwargs["verbosity"] == expected


@pytest.mark.parametrize(
    "argv", [["-q", "status"], ["status", "-q"], ["status", "--quiet"]]
)
def test_quiet_accepted_before_or_after_subcommand(argv):
    with (
        mock.patch.object(cli, "configure_logging") as cfg,
        mock.patch.object(cli, "parse_config", return_value=None),
    ):
        runner.invoke(app, argv)
    assert cfg.call_args.kwargs["quiet"] is True


def test_global_flag_hoisted_through_nested_subcommand():
    """A global buried after a *nested* ``snapshot list <vm>`` still reaches the
    root — only the top app carries :class:`~tkc_lvlab.cli.GlobalFlagGroup`, but
    the reorder runs on the full ``argv`` before the subcommand chain is split."""
    with (
        mock.patch.object(cli, "configure_logging"),
        mock.patch.object(cli, "parse_config", return_value=None),
    ):
        res = runner.invoke(app, ["snapshot", "list", "whatever", "--no-color"])
    assert "No such option" not in res.output
    assert output.color_disabled() is True


def test_unknown_post_subcommand_flag_still_errors():
    """Hoisting is scoped to the known globals — an unrecognized flag after the
    subcommand remains a hard parse error rather than being silently swallowed."""
    res = runner.invoke(app, ["status", "--definitely-not-a-flag"])
    assert res.exit_code == 2
    assert "No such option" in res.output
