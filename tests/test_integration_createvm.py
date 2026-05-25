"""Integration tests for the standalone ``createvm`` / ``deletevm`` round-trip.

Gated by ``LVLAB_INTEGRATION=1``. The default test run skips every
function in this module via the ``integration`` marker. See
``tests/conftest.py`` and the "Integration tests" safety rules in
``CLAUDE.md`` before adding to this file.

``createvm`` / ``deletevm`` are faithful ports of the ``lvscripts``
commands: positional ``VM_NAME`` / ``VM_DISTRO``, always-copy disk, and a
**qemu:///system** target (no ``--uri`` / user-mode networking). These
tests therefore run on the system URI only; the parametrized session URI
is skipped.

Every libvirt domain, qcow2 file, and cloud-init ISO this module creates
is named via :func:`make_test_name` so the session reaper can clean up
after a crashing test. Because the scripts use raw libvirt domain names,
the domain name IS the prefixed ``make_test_name`` value. Per-VM storage
lives under the dedicated ``/var/lib/libvirt/images/lvlab-test/`` directory
(via ``--storage-root``), distinct from the production
``/var/lib/libvirt/images/lvlab/oneoff/``.

The cloud-image cache at ``/var/lib/libvirt/images/lvlab/cloud-images/`` is
intentionally shared with normal lvlab usage — the cache is read-only after
download, and re-downloading a 400 MB qcow2 every run would make the suite
hostile to iterate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import find_entry_point, list_domains, wait_for_no_domain


_CREATEVM_TIMEOUT_SECONDS = 300
_DESTROYVM_TIMEOUT_SECONDS = 60


@pytest.mark.integration
def test_createvm_deletevm_roundtrip(
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    test_ssh_pubkey_path: Path,
) -> None:
    """``createvm`` defines a domain; ``deletevm --force`` undefines it.

    End-to-end exercises the standalone scripts: real ``virt-install``,
    real qcow2 disk, real cloud-init ISO, real libvirt domain definition.
    The per-VM qcow2 is always a standalone copy (no backing-file tie to
    the shared cache) and ``--storage-root`` points at the dedicated test
    storage location.

    The cloud image (``debian13``) is fetched into the shared
    ``/var/lib/libvirt/images/lvlab/cloud-images/`` cache on first run; the
    test does not wipe it.

    Args:
        integration_uri: libvirt URI parametrized by the ``integration_uri``
            fixture. ``createvm`` targets qemu:///system, so session URIs
            are skipped.
        lvlab_integration_storage_root: libvirt-readable test storage root
            (``/var/lib/libvirt/images/lvlab-test/``).
        test_ssh_pubkey_path: Path to a throwaway SSH public key.
    """
    if "session" in integration_uri:
        pytest.skip("createvm targets qemu:///system only")

    createvm = find_entry_point("createvm")
    deletevm = find_entry_point("deletevm")

    vm_name = make_test_name("createvm-roundtrip")
    # Raw-name contract: the libvirt domain is exactly vm_name (which
    # already carries LVLAB_TEST_PREFIX from make_test_name).
    expected_domain = vm_name
    assert_owned_by_test(expected_domain)

    storage_root = lvlab_integration_storage_root

    create_result = subprocess.run(
        [
            createvm,
            vm_name,
            "debian13",
            "--storage-root",
            str(storage_root),
            "--public-key",
            str(test_ssh_pubkey_path),
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

        assert expected_domain in list_domains(integration_uri), (
            f"createvm reported success but domain {expected_domain!r} "
            f"is not in virsh list on {integration_uri}"
        )

        per_vm_dir = storage_root / vm_name
        assert per_vm_dir.is_dir(), (
            f"createvm reported success but storage dir {per_vm_dir} "
            f"was not created"
        )
        assert (
            per_vm_dir / "disk0.qcow2"
        ).is_file(), (
            f"createvm reported success but {per_vm_dir / 'disk0.qcow2'} is missing"
        )
        assert (
            per_vm_dir / "cidata.iso"
        ).is_file(), (
            f"createvm reported success but {per_vm_dir / 'cidata.iso'} is missing"
        )
    finally:
        # deletevm must always run — even if the assertions above failed,
        # we want to leave the host clean. The session reaper is a safety
        # net, not a substitute for explicit cleanup.
        destroy_result = subprocess.run(
            [
                deletevm,
                vm_name,
                "--force",
                "--storage-root",
                str(storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROYVM_TIMEOUT_SECONDS,
        )

    assert destroy_result.returncode == 0, (
        f"deletevm failed (exit {destroy_result.returncode}):\n"
        f"stdout:\n{destroy_result.stdout}\n"
        f"stderr:\n{destroy_result.stderr}"
    )

    wait_for_no_domain(integration_uri, expected_domain)
