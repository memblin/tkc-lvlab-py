"""Integration tests for the standalone ``createvm`` / ``destroyvm`` round-trip.

Gated by ``LVLAB_INTEGRATION=1``. The default test run skips every
function in this module via the ``integration`` marker. See
``tests/conftest.py`` and the "Cross-cutting safety rules" section of
``TODO.md`` before adding to this file.

Every libvirt domain, qcow2 file, and cloud-init ISO this module
creates is named via :func:`make_test_name` so the session reaper can
clean up after a crashing test. Storage lives under
:func:`lvlab_test_basedir` (a pytest ``tmp_path`` directory) so even
unmanaged qcow2s are auto-removed when the test session ends.

The cloud-image cache at ``/var/lib/libvirt/images/cloud-images/`` is
intentionally shared with the developer's normal lvlab usage — the
cache is read-only after download, and forcing tests to re-download a
432 MB qcow2 every run would make the suite hostile to iterate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name


_VIRSH_TIMEOUT_SECONDS = 30
_CREATEVM_TIMEOUT_SECONDS = 300
_DESTROYVM_TIMEOUT_SECONDS = 60
_DOMAIN_GONE_POLL_SECONDS = 0.5
_DOMAIN_GONE_TIMEOUT_SECONDS = 20


def _run_virsh(uri: str, *args: str) -> subprocess.CompletedProcess[str]:
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
        timeout=_VIRSH_TIMEOUT_SECONDS,
        env={**os.environ, "LC_ALL": "C"},
    )


def _list_domains(uri: str) -> list[str]:
    """Return the list of every defined libvirt domain name on ``uri``.

    Args:
        uri: libvirt URI to query.

    Returns:
        Domain names (running and stopped). Empty list if the listing
        command failed; the caller should treat that as a test
        environment problem.
    """
    result = _run_virsh(uri, "list", "--all", "--name")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _wait_for_no_domain(uri: str, domain_name: str) -> None:
    """Poll ``virsh list --all --name`` until ``domain_name`` is absent.

    Args:
        uri: libvirt URI to query.
        domain_name: The fully-qualified domain name to wait for.

    Raises:
        AssertionError: ``domain_name`` is still present after
            :data:`_DOMAIN_GONE_TIMEOUT_SECONDS`.
    """
    deadline = time.monotonic() + _DOMAIN_GONE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if domain_name not in _list_domains(uri):
            return
        time.sleep(_DOMAIN_GONE_POLL_SECONDS)
    raise AssertionError(
        f"Domain {domain_name!r} still present on {uri} after "
        f"{_DOMAIN_GONE_TIMEOUT_SECONDS}s — destroyvm did not converge"
    )


def _find_entry_point(name: str) -> str:
    """Resolve a console-script entry point's absolute path.

    ``uv run pytest`` puts ``.venv/bin`` on PATH, so the
    ``createvm`` / ``destroyvm`` scripts installed by ``uv sync``
    resolve via ``shutil.which``. Fall back to ``pytest.fail`` (not
    ``pytest.skip``) if the entry point is missing — that's a broken
    test environment, not a runtime skip condition.

    Args:
        name: Console-script name (``"createvm"`` or ``"destroyvm"``).

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


@pytest.mark.integration
def test_createvm_destroyvm_roundtrip(
    integration_uri: str,
    lvlab_test_basedir: Path,
) -> None:
    """``createvm`` defines a domain; ``destroyvm --force`` undefines it.

    End-to-end exercises the standalone scripts: real ``virt-install``,
    real qcow2 disk, real cloud-init ISO, real libvirt domain
    definition. Uses ``--copy`` so the per-VM qcow2 is standalone (no
    backing-file tie to the shared cloud-images cache) and
    ``--storage-root <tmp>`` so per-VM artifacts land in the pytest
    tmp directory.

    The cloud image (``debian13``) is fetched into the shared
    ``/var/lib/libvirt/images/cloud-images/`` cache on first run; the
    test does not wipe it.

    Args:
        integration_uri: libvirt URI parametrized by the
            :func:`integration_uri` fixture (skipped per-URI if not
            reachable).
        lvlab_test_basedir: Session-scoped pytest tmp directory for the
            per-VM ``--storage-root``.
    """
    createvm = _find_entry_point("createvm")
    destroyvm = _find_entry_point("destroyvm")

    vm_name = make_test_name("createvm-roundtrip")
    expected_domain = f"oneoff-{vm_name}"
    assert_owned_by_test(expected_domain)

    # Per-test storage root so the per-VM directory does not collide
    # with another parametrized run sharing the session tmpdir.
    storage_root = lvlab_test_basedir / f"createvm-{integration_uri.replace('/', '_')}"

    create_result = subprocess.run(
        [
            createvm,
            vm_name,
            "--distro",
            "debian13",
            "--uri",
            integration_uri,
            "--storage-root",
            str(storage_root),
            "--copy",
            "--memory",
            "1024",
            "--cpu",
            "1",
            "--disk-size",
            "5G",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_CREATEVM_TIMEOUT_SECONDS,
    )

    try:
        assert create_result.returncode == 0, (
            f"createvm failed (exit {create_result.returncode}):\n"
            f"stdout:\n{create_result.stdout}\n"
            f"stderr:\n{create_result.stderr}"
        )

        assert expected_domain in _list_domains(integration_uri), (
            f"createvm reported success but domain {expected_domain!r} "
            f"is not in virsh list on {integration_uri}"
        )

        per_vm_dir = storage_root / vm_name
        assert per_vm_dir.is_dir(), (
            f"createvm reported success but storage dir {per_vm_dir} "
            f"was not created"
        )
        assert (per_vm_dir / "disk0.qcow2").is_file(), (
            f"createvm reported success but {per_vm_dir / 'disk0.qcow2'} " f"is missing"
        )
        assert (per_vm_dir / "cidata.iso").is_file(), (
            f"createvm reported success but {per_vm_dir / 'cidata.iso'} " f"is missing"
        )
    finally:
        # destroyvm must always run — even if the assertions above
        # failed, we want to leave the host clean. The session reaper
        # is a safety net, not a substitute for explicit cleanup.
        destroy_result = subprocess.run(
            [
                destroyvm,
                vm_name,
                "--force",
                "--uri",
                integration_uri,
                "--storage-root",
                str(storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROYVM_TIMEOUT_SECONDS,
        )

    assert destroy_result.returncode == 0, (
        f"destroyvm failed (exit {destroy_result.returncode}):\n"
        f"stdout:\n{destroy_result.stdout}\n"
        f"stderr:\n{destroy_result.stderr}"
    )

    _wait_for_no_domain(integration_uri, expected_domain)
