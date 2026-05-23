"""Shared helpers for the integration test bodies.

Module-private to the test suite. Pytest does not collect anything from
this file because its filename does not match the ``test_*.py``
discovery pattern; the helpers are imported explicitly by the
``test_integration_*.py`` modules.

Helpers here are deliberately thin wrappers around ``subprocess.run``
and ``virsh`` — no fixtures, no pytest marks. Fixtures live in
``tests/conftest.py``; this module exists so the same subprocess +
polling shapes don't need to be duplicated across every integration
test file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest


VIRSH_TIMEOUT_SECONDS = 30
"""Default timeout for short virsh probes used in test helpers."""

DOMAIN_GONE_POLL_SECONDS = 0.5
"""Seconds between successive ``virsh list`` polls in :func:`wait_for_no_domain`."""

DOMAIN_GONE_TIMEOUT_SECONDS = 20
"""Total seconds :func:`wait_for_no_domain` will wait before raising."""


MANIFEST_TEMPLATE = dedent(
    """\
    ---
    environment:
      - name: {env_name}
        libvirt_uri: {uri}
        config_defaults:
          domain: local
          os: debian13
          cpu: 1
          memory: 1024
          disks:
            - name: primary
              size: 5G
          interfaces:
            network: default
            network_type: {network_type}
          cloud_image_basedir: /var/lib/libvirt/images
          disk_image_basedir: {storage_root}
          cloud_init:
            user: root
            pubkey: {pubkey_path}
            sudo:
              - ALL=(ALL) NOPASSWD:ALL
            shell: /bin/bash
        machines:
          - vm_name: {vm_name}
            hostname: testhost
            interfaces:
              - name: eth0

    images:
      debian13:
        image_url: https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2
        checksum_url: https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
        checksum_type: sha512
        network_version: 2
    """
)
"""Minimal single-machine ``Lvlab.yml`` template for integration tests.

Placeholders: ``env_name``, ``uri``, ``storage_root``, ``vm_name``,
``pubkey_path``, ``network_type``. Tests render this with
:func:`render_manifest`. The template declares exactly one machine;
tests that need a different shape (e.g. multiple machines) should not
extend this helper — copy and modify in the test file instead.
"""


def network_type_for_uri(uri: str) -> str:
    """Return the manifest ``interfaces.network_type`` appropriate for ``uri``.

    Phase 12 invariant: ``qemu:///session`` uses user-mode networking
    (no libvirt network needed); ``qemu:///system`` uses the managed
    libvirt ``default`` network. Tests pick the value via the URI tag
    so the same test body runs on both URIs.

    Args:
        uri: libvirt URI string (typically from the ``integration_uri``
            fixture).

    Returns:
        ``"user"`` when the URI contains ``"session"``; ``"network"``
        otherwise.
    """
    return "user" if "session" in uri else "network"


def createvm_network_args(uri: str) -> list[str]:
    """Return the ``--network-type`` argv fragment createvm needs for ``uri``.

    Mirror of :func:`network_type_for_uri` for the standalone
    ``createvm`` surface. Tests spread the return value into the
    subprocess argv so the same call works for both URIs.

    Args:
        uri: libvirt URI string.

    Returns:
        Two-element argv list ``["--network-type", <value>]``.
    """
    return ["--network-type", network_type_for_uri(uri)]


def render_manifest(
    *,
    env_name: str,
    uri: str,
    storage_root: Path,
    vm_name: str,
    pubkey_path: Path,
    network_type: str | None = None,
) -> str:
    """Render the integration-test manifest YAML with per-run values filled in.

    Args:
        env_name: Prefixed environment name (lvlab uses it as the
            domain-name suffix and as a storage-path component).
        uri: libvirt URI selected by the ``integration_uri`` fixture.
        storage_root: ``disk_image_basedir`` — the test storage root
            exposed by ``lvlab_integration_storage_root``.
        vm_name: Prefixed VM name (becomes the domain-name prefix and
            the per-VM storage subdir).
        pubkey_path: Absolute path to an existing SSH public key on
            the test host.
        network_type: ``interfaces.network_type`` value. When ``None``
            (default) the value is derived from ``uri`` via
            :func:`network_type_for_uri` — session URIs get user-mode,
            system URIs get the managed libvirt network. Pass an
            explicit value to override.

    Returns:
        A complete ``Lvlab.yml`` YAML document declaring one machine.
    """
    return MANIFEST_TEMPLATE.format(
        env_name=env_name,
        uri=uri,
        storage_root=storage_root,
        vm_name=vm_name,
        pubkey_path=pubkey_path,
        network_type=network_type or network_type_for_uri(uri),
    )


def run_virsh(uri: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``virsh -c <uri> <args>`` with locale-stable env, returning the result.

    Args:
        uri: libvirt URI to operate against.
        *args: Remaining ``virsh`` arguments.

    Returns:
        The :class:`subprocess.CompletedProcess` — caller decides whether
        non-zero exit is fatal.
    """
    return subprocess.run(
        ["virsh", "-c", uri, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=VIRSH_TIMEOUT_SECONDS,
        env={**os.environ, "LC_ALL": "C"},
    )


def list_domains(uri: str) -> list[str]:
    """Return the list of every defined libvirt domain name on ``uri``.

    Args:
        uri: libvirt URI to query.

    Returns:
        Domain names (running and stopped). Empty list if the listing
        command failed; the caller should treat that as a test
        environment problem.
    """
    result = run_virsh(uri, "list", "--all", "--name")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def wait_for_no_domain(uri: str, domain_name: str) -> None:
    """Poll ``virsh list --all --name`` until ``domain_name`` is absent.

    Args:
        uri: libvirt URI to query.
        domain_name: The fully-qualified domain name to wait for.

    Raises:
        AssertionError: ``domain_name`` is still present after
            :data:`DOMAIN_GONE_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + DOMAIN_GONE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if domain_name not in list_domains(uri):
            return
        time.sleep(DOMAIN_GONE_POLL_SECONDS)
    raise AssertionError(
        f"Domain {domain_name!r} still present on {uri} after "
        f"{DOMAIN_GONE_TIMEOUT_SECONDS}s — destroy did not converge"
    )


def find_entry_point(name: str) -> str:
    """Resolve a console-script entry point's absolute path.

    ``uv run pytest`` puts ``.venv/bin`` on PATH, so the
    ``lvlab`` / ``createvm`` / ``destroyvm`` scripts installed by
    ``uv sync`` resolve via ``shutil.which``. Fall back to
    :func:`pytest.fail` (not :func:`pytest.skip`) if the entry point is
    missing — that's a broken test environment, not a runtime skip
    condition.

    Args:
        name: Console-script name (``"lvlab"``, ``"createvm"``, or
            ``"destroyvm"``).

    Returns:
        Absolute path to the executable.
    """
    found = shutil.which(name)
    if found is None:
        pytest.fail(
            f"console script {name!r} not found on PATH — run "
            f"'uv sync' before invoking integration tests"
        )
    return found
