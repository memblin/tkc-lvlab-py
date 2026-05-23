"""Unit tests for the ``lvlab capabilities`` CLI command.

These tests stub :func:`tkc_lvlab.cli.virsh_capabilities` at the import
boundary so nothing here ever invokes ``virsh`` or the libvirt-python
binding. They lock in the Phase 2 port: ``capabilities`` must go through
the virsh wrapper, never through :func:`connect_to_libvirt`.
"""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import capabilities
from tkc_lvlab.utils.virsh import VirshError


SAMPLE_CAPS_XML = (
    "<capabilities>\n"
    "  <host>\n"
    "    <uuid>00000000-0000-0000-0000-000000000000</uuid>\n"
    "  </host>\n"
    "</capabilities>\n"
)


def test_capabilities_prints_xml_from_virsh_capabilities() -> None:
    """Happy path: stdout contains the banner and the XML from virsh."""
    runner = CliRunner()
    with mock.patch.object(
        cli, "virsh_capabilities", return_value=SAMPLE_CAPS_XML
    ) as mocked:
        result = runner.invoke(capabilities, [])

    assert result.exit_code == 0, result.output
    assert "Capabilities:" in result.output
    assert "<capabilities>" in result.output
    assert "00000000-0000-0000-0000-000000000000" in result.output
    # Locks in the Phase 2 design decision: default URI is qemu:///session.
    mocked.assert_called_once_with("qemu:///session")


def test_capabilities_does_not_reach_libvirt_python() -> None:
    """Regression guard: command must not call connect_to_libvirt.

    ``cli.py`` stopped importing ``connect_to_libvirt`` as of Phase 2
    step 5 (status command rewrite), so we patch the helper on the
    ``tkc_lvlab.utils.libvirt`` module where it still lives (step 6
    will delete it). The patch will start to AttributeError once that
    deletion lands — at which point the regression guard has lost its
    target and this whole test can be retired with the function.
    """
    runner = CliRunner()
    with (
        mock.patch.object(cli, "virsh_capabilities", return_value=SAMPLE_CAPS_XML),
        mock.patch("tkc_lvlab.utils.libvirt.connect_to_libvirt") as connect_mock,
    ):
        result = runner.invoke(capabilities, [])

    assert result.exit_code == 0, result.output
    connect_mock.assert_not_called()


def test_capabilities_virsh_error_exits_nonzero_with_stderr_message() -> None:
    """When virsh fails, the CLI exits nonzero and surfaces the error."""
    err = VirshError(1, "error: failed to connect to the hypervisor", ["capabilities"])
    runner = CliRunner()
    with mock.patch.object(cli, "virsh_capabilities", side_effect=err):
        result = runner.invoke(capabilities, [])

    assert result.exit_code == 1
    # The error message goes to stderr, not stdout, and the success banner
    # must not have been printed. Click 8.2+ always separates stderr.
    assert "Capabilities:" not in result.stdout
    assert "failed to connect to the hypervisor" in result.stderr
