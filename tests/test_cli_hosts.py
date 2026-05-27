"""Unit tests for the ``lvlab hosts`` CLI command.

Locks the two-mode (default / ``--append``) + ``--heredoc`` behaviour
of ``hosts`` so the cognitive-complexity refactor of the command body
can move with a safety net. The command body had 0% coverage before.

All filesystem interactions are mocked — no real ``/etc/hosts`` is
touched, opened, or read from.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app
from tkc_lvlab.exceptions import ConfigError

SNIPPET_DEFAULT = "## hosts snippet (default mode)\n10.0.0.1 web01.lab web01\n"
SNIPPET_HEREDOC = "cat <<EOF >> /etc/hosts\n10.0.0.1 web01.lab web01\nEOF\n"


def _stub_parse_config() -> tuple[dict, dict, dict, list]:
    return (
        {"name": "test-env"},
        {},
        {"domain": "lab"},
        [
            {
                "vm_name": "web01",
                "hostname": "web01",
                "interfaces": [{"ip4": "10.0.0.1/24"}],
            }
        ],
    )


def _run_hosts(argv: list[str], **patches) -> "object":
    """Invoke ``lvlab hosts`` with parse_config + generate_hosts patched.

    Extra patches (e.g. parse_hosts_file, generate_hosts_entries) are
    layered on top via the ``patches`` kwarg map: ``{attr_name: mock_or_value}``.
    """
    runner = CliRunner()
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(cli, "parse_config", return_value=_stub_parse_config())
        )
        gen_hosts_mock = stack.enter_context(
            mock.patch.object(
                cli,
                "generate_hosts",
                side_effect=lambda env, defs, mach, heredoc=None: (
                    SNIPPET_HEREDOC if heredoc else SNIPPET_DEFAULT
                ),
            )
        )
        # capture the gen_hosts_mock so callers can assert on it
        patches["_gen_hosts_mock"] = gen_hosts_mock
        for attr, value in patches.items():
            if attr.startswith("_"):
                continue
            if isinstance(value, mock.Mock):
                stack.enter_context(mock.patch.object(cli, attr, value))
            else:
                stack.enter_context(mock.patch.object(cli, attr, return_value=value))
        result = runner.invoke(app, ["hosts", *argv])
        result.gen_hosts_mock = gen_hosts_mock  # type: ignore[attr-defined]
        return result


def test_hosts_default_mode_prints_snippet() -> None:
    """No flags → just print the default-mode snippet to stdout."""
    result = _run_hosts([])
    assert result.exit_code == 0, result.output
    assert SNIPPET_DEFAULT in result.output


def test_hosts_heredoc_passes_heredoc_kwarg_to_generate_hosts() -> None:
    """``--heredoc`` → snippet is rendered in heredoc mode."""
    result = _run_hosts(["--heredoc"])
    assert result.exit_code == 0, result.output
    assert SNIPPET_HEREDOC in result.output
    # generate_hosts must have been called with heredoc set to a truthy value.
    calls = result.gen_hosts_mock.call_args_list
    assert any(c.kwargs.get("heredoc") for c in calls), calls


def test_hosts_append_writes_non_conflicting_entries(tmp_path) -> None:
    """``--append`` with no conflicts → header + each entry written, each echoed."""
    target = tmp_path / "etc_hosts"
    target.write_text("127.0.0.1 localhost\n")  # exists, writable
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(cli, "generate_hosts", return_value=SNIPPET_DEFAULT),
        mock.patch.object(
            cli,
            "generate_hosts_entries",
            return_value=[
                {"ip4": "10.0.0.1", "fqdn": "web01.lab", "hostname": "web01"}
            ],
        ),
        mock.patch.object(cli, "parse_hosts_file", return_value=(set(), set())),
        # Redirect the /etc/hosts target by patching the literal inside the
        # command. Easiest hook: monkeypatch open() so writes land in tmp.
        mock.patch("builtins.open", mock.mock_open()) as mocked_open,
        mock.patch("os.access", return_value=True),
    ):
        result = runner.invoke(app, ["hosts", "--append"])

    assert result.exit_code == 0, result.output
    assert "Appended: 10.0.0.1 web01.lab web01" in result.output
    # builtins.open was called for "/etc/hosts" in append mode.
    open_paths = [c.args[0] for c in mocked_open.call_args_list]
    assert "/etc/hosts" in open_paths


def test_hosts_append_reports_skips_when_entries_conflict() -> None:
    """``--append`` with all entries conflicting → echo skip reasons + "No new entries"."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(cli, "generate_hosts", return_value=SNIPPET_DEFAULT),
        mock.patch.object(
            cli,
            "generate_hosts_entries",
            return_value=[
                {"ip4": "10.0.0.1", "fqdn": "web01.lab", "hostname": "web01"}
            ],
        ),
        mock.patch.object(
            cli,
            "parse_hosts_file",
            return_value=({"10.0.0.1"}, {"web01", "web01.lab"}),
        ),
    ):
        result = runner.invoke(app, ["hosts", "--append"])

    assert result.exit_code == 0, result.output
    assert "Skipping 10.0.0.1 web01.lab web01" in result.output
    assert "IP 10.0.0.1 already present" in result.output
    assert "No new entries to append to /etc/hosts." in result.output


def test_hosts_append_logs_error_when_etc_hosts_unreadable() -> None:
    """``parse_hosts_file`` raising OSError → log + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(cli, "generate_hosts", return_value=SNIPPET_DEFAULT),
        mock.patch.object(
            cli, "parse_hosts_file", side_effect=PermissionError("denied")
        ),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["hosts", "--append"])

    assert result.exit_code == 1, result.output
    error_calls = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("Unable to read" in fmt for fmt in error_calls), error_calls


def test_hosts_append_logs_error_when_no_write_access() -> None:
    """``--append`` with non-writable /etc/hosts → error log, no crash."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", return_value=_stub_parse_config()),
        mock.patch.object(cli, "generate_hosts", return_value=SNIPPET_DEFAULT),
        mock.patch.object(
            cli,
            "generate_hosts_entries",
            return_value=[
                {"ip4": "10.0.0.1", "fqdn": "web01.lab", "hostname": "web01"}
            ],
        ),
        mock.patch.object(cli, "parse_hosts_file", return_value=(set(), set())),
        mock.patch("os.access", return_value=False),
        mock.patch("os.path.exists", return_value=True),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["hosts", "--append"])

    assert result.exit_code == 0, result.output
    error_calls = [c.args[0] for c in mocked_logger.error.call_args_list]
    assert any("No write access" in msg for msg in error_calls), error_calls
    # The default snippet should still print at the end.
    assert SNIPPET_DEFAULT in result.output


def test_hosts_handles_parse_config_typeerror() -> None:
    """parse_config raising TypeError (missing-file unpack) → error log + exit 1.

    The bare ``sys.exit()`` (exit 0) the hosts command used to call on a
    parse failure was standardized to ``typer.Exit(code=1)`` so a failed
    parse no longer looks like a success to the shell — matching every
    other lvlab subcommand.
    """
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["hosts"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")


def test_hosts_handles_parse_config_configerror() -> None:
    """parse_config raising ConfigError (bad structure) → error log + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=ConfigError("boom")),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["hosts"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")
