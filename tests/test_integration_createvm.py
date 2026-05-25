"""Integration tests for the standalone ``createvm`` / ``destroyvm`` round-trip.

Gated by ``LVLAB_INTEGRATION=1``. The default test run skips every
function in this module via the ``integration`` marker. See
``tests/conftest.py`` and the "Integration tests" safety rules in
``CLAUDE.md`` before adding to this file.

Every libvirt domain, qcow2 file, and cloud-init ISO this module
creates is named via :func:`make_test_name` so the session reaper can
clean up after a crashing test. Because ``createvm`` / ``destroyvm`` now
use raw libvirt domain names, the domain name IS the prefixed
``make_test_name`` value — the reaper still recognizes it. Storage lives
under the dedicated ``/var/lib/libvirt/images/lvlab-test/`` directory
(exposed via the :func:`lvlab_integration_storage_root` fixture),
distinct from the production ``/var/lib/libvirt/images/lvlab/oneoff/``
that real users' VMs occupy. ``createvm`` refuses to overwrite an
existing per-VM dir (``mkdir(exist_ok=False)`` in the script), so a
leftover prefixed directory from a crashed prior run becomes a loud
failure rather than silent state corruption.

The cloud-image cache at ``/var/lib/libvirt/images/lvlab/cloud-images/``
is intentionally shared with the developer's normal lvlab usage — the
cache is read-only after download, and forcing tests to re-download a
432 MB qcow2 every run would make the suite hostile to iterate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import (
    createvm_network_args,
    find_entry_point,
    list_domains,
    wait_for_no_domain,
)


_CREATEVM_TIMEOUT_SECONDS = 300
_DESTROYVM_TIMEOUT_SECONDS = 60


@pytest.mark.integration
def test_createvm_destroyvm_roundtrip(
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    test_ssh_pubkey_path: Path,
) -> None:
    """``createvm`` defines a domain; ``destroyvm --force`` undefines it.

    End-to-end exercises the standalone scripts: real ``virt-install``,
    real qcow2 disk, real cloud-init ISO, real libvirt domain
    definition. Uses ``--copy`` so the per-VM qcow2 is standalone (no
    backing-file tie to the shared cloud-images cache) and
    ``--storage-root`` set to the dedicated test storage location so
    per-VM artifacts land outside the production
    ``/var/lib/libvirt/images/lvlab/oneoff/`` directory.

    The cloud image (``debian13``) is fetched into the shared
    ``/var/lib/libvirt/images/lvlab/cloud-images/`` cache on first run;
    the test does not wipe it.

    Args:
        integration_uri: libvirt URI parametrized by the
            :func:`integration_uri` fixture (skipped per-URI if not
            test-ready).
        lvlab_integration_storage_root: libvirt-readable test storage
            root (``/var/lib/libvirt/images/lvlab-test/``).
    """
    createvm = find_entry_point("createvm")
    destroyvm = find_entry_point("destroyvm")

    # Distinguish parametrized URI runs so each gets its own per-VM
    # name (and thus its own per-VM dir). ``make_test_name`` keeps the
    # LVLAB_TEST_PREFIX so both the domain reaper and the storage
    # reaper still recognize it.
    uri_tag = "session" if "session" in integration_uri else "system"
    vm_name = make_test_name(f"createvm-roundtrip-{uri_tag}")
    # Raw-name contract: the libvirt domain is exactly vm_name (which
    # already carries LVLAB_TEST_PREFIX from make_test_name).
    expected_domain = vm_name
    assert_owned_by_test(expected_domain)

    storage_root = lvlab_integration_storage_root

    create_result = subprocess.run(
        [
            createvm,
            vm_name,
            "--distro",
            "debian13",
            "--uri",
            integration_uri,
            *createvm_network_args(integration_uri),
            "--storage-root",
            str(storage_root),
            "--public-key",
            str(test_ssh_pubkey_path),
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

        assert expected_domain in list_domains(integration_uri), (
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

    wait_for_no_domain(integration_uri, expected_domain)
