"""Unit tests for the ``lvlab global show instances`` CLI command.

These tests stub the virsh enumeration helpers and :func:`parse_config` at
the ``tkc_lvlab.cli`` import boundary so nothing here ever invokes ``virsh``
or libvirt. They lock in the cross-connection behaviour: domains from every
reachable connection appear in one table, an unreachable connection is skipped
without failing the command, and an ``In manifest`` column appears only when a
manifest is present in the working directory.
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app
from tkc_lvlab.utils.virsh import DomInfo, VirshError

# Domains keyed by URI for the enumeration stubs. The two default URIs return
# disjoint domain sets so a test can prove both connections are merged.
SYSTEM_DOMAINS = ["web01_demo", "db01_demo"]
SESSION_DOMAINS = ["scratch_session"]

DOMINFO_BY_NAME = {
    "web01_demo": DomInfo(
        state="running",
        vcpus=2,
        max_memory_kib=2097152,
        autostart=True,
        persistent=True,
    ),
    "db01_demo": DomInfo(
        state="shut off",
        vcpus=4,
        max_memory_kib=4194304,
        autostart=False,
        persistent=True,
    ),
    "scratch_session": DomInfo(
        state="running",
        vcpus=1,
        max_memory_kib=1048576,
        autostart=False,
        persistent=False,
    ),
}


def _list_side_effect(domains_by_uri: dict[str, list[str]]):
    """Return a virsh_list_all_names stub keyed on the connection URI."""

    def _side(uri: str) -> list[str]:
        if uri not in domains_by_uri:
            raise AssertionError(f"unexpected list call for {uri}")
        return domains_by_uri[uri]

    return _side


def _dominfo_side(uri: str, name: str) -> DomInfo:
    """Return the canned DomInfo for ``name`` regardless of connection."""
    return DOMINFO_BY_NAME[name]


def test_instances_merges_domains_from_both_connections() -> None:
    """Domains from qemu:///system AND qemu:///session both land in the table."""
    runner = CliRunner()
    domains = {
        "qemu:///system": SYSTEM_DOMAINS,
        "qemu:///session": SESSION_DOMAINS,
    }
    with (
        # No manifest in CWD -> no In-manifest column for this case.
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(
            cli, "virsh_list_all_names", side_effect=_list_side_effect(domains)
        ),
        mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
    ):
        result = runner.invoke(app, ["global", "show", "instances"])

    assert result.exit_code == 0, result.output
    # Every domain from both connections is present.
    assert "web01_demo" in result.output
    assert "db01_demo" in result.output
    assert "scratch_session" in result.output
    # Both URIs are rendered as the connection column.
    assert "qemu:///system" in result.output
    assert "qemu:///session" in result.output
    # Cheap facts surface (state + autostart + persistent).
    assert "running" in result.output
    assert "shut off" in result.output


def test_instances_unreachable_connection_is_skipped_not_fatal() -> None:
    """A connection that errors is skipped with a note; the others still render."""
    runner = CliRunner()

    def list_side(uri: str) -> list[str]:
        if uri == "qemu:///session":
            raise VirshError(1, "failed to connect to the hypervisor", ["list"])
        if uri == "qemu:///system":
            return SYSTEM_DOMAINS
        raise AssertionError(f"unexpected list call for {uri}")

    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(cli, "virsh_list_all_names", side_effect=list_side),
        mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
    ):
        result = runner.invoke(app, ["global", "show", "instances"])

    # The command does NOT fail just because one connection was unreachable.
    assert result.exit_code == 0, result.output
    # The reachable connection's domains still render.
    assert "web01_demo" in result.output
    # The unreachable one is surfaced as a skip note naming the URI.
    assert "Skipping unreachable connection qemu:///session" in result.output


def test_instances_in_manifest_column_when_manifest_present() -> None:
    """With an Lvlab.yml present, an In-manifest column flags matching domains."""
    runner = CliRunner()
    # Manifest defines web01 in env "demo" -> domain web01_demo (a match) and
    # a vm with no live domain. scratch_session is NOT in the manifest.
    environment = {"name": "demo", "libvirt_uri": "qemu:///session"}
    machines = [{"vm_name": "web01"}, {"vm_name": "db01"}]
    parsed = (environment, {}, {}, machines)

    domains = {
        "qemu:///system": ["web01_demo"],
        "qemu:///session": ["scratch_session"],
    }
    with (
        mock.patch.object(cli, "parse_config", return_value=parsed),
        mock.patch.object(
            cli, "virsh_list_all_names", side_effect=_list_side_effect(domains)
        ),
        mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
    ):
        result = runner.invoke(app, ["global", "show", "instances"])

    assert result.exit_code == 0, result.output
    # The In-manifest column header appears (Rich may wrap; match the words).
    assert "In manifest" in result.output or "In\nmanifest" in result.output
    # Both yes (web01_demo matches a manifest machine) and no (scratch_session
    # is not in the manifest) verdicts are present.
    assert "yes" in result.output
    assert "no" in result.output


def test_instances_no_manifest_omits_in_manifest_column() -> None:
    """Without a manifest, the In-manifest column is omitted entirely."""
    runner = CliRunner()
    domains = {
        "qemu:///system": SYSTEM_DOMAINS,
        "qemu:///session": [],
    }
    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(
            cli, "virsh_list_all_names", side_effect=_list_side_effect(domains)
        ),
        mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
    ):
        result = runner.invoke(app, ["global", "show", "instances"])

    assert result.exit_code == 0, result.output
    assert "In manifest" not in result.output


def test_instances_extra_uri_is_enumerated() -> None:
    """A repeatable --uri adds a connection beyond the default pair."""
    runner = CliRunner()
    domains = {
        "qemu:///system": [],
        "qemu:///session": [],
        "qemu+ssh://host/system": ["remote01"],
    }
    DOMINFO_BY_NAME["remote01"] = DomInfo(
        state="running",
        vcpus=8,
        max_memory_kib=8388608,
        autostart=True,
        persistent=True,
    )
    try:
        with (
            mock.patch.object(cli, "parse_config", return_value=None),
            mock.patch.object(
                cli, "virsh_list_all_names", side_effect=_list_side_effect(domains)
            ),
            mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
        ):
            result = runner.invoke(
                app,
                ["global", "show", "instances", "--uri", "qemu+ssh://host/system"],
            )
    finally:
        del DOMINFO_BY_NAME["remote01"]

    assert result.exit_code == 0, result.output
    assert "remote01" in result.output
    assert "qemu+ssh://host/system" in result.output


def test_instances_all_connections_empty_reports_none() -> None:
    """When no reachable connection has domains, a friendly empty note prints."""
    runner = CliRunner()
    domains = {"qemu:///system": [], "qemu:///session": []}
    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(
            cli, "virsh_list_all_names", side_effect=_list_side_effect(domains)
        ),
        mock.patch.object(cli, "virsh_dominfo", side_effect=_dominfo_side),
    ):
        result = runner.invoke(app, ["global", "show", "instances"])

    assert result.exit_code == 0, result.output
    assert "No instances found" in result.output
