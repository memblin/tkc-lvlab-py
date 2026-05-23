"""Integration tests for the ``lvlab`` manifest round-trip.

Gated by ``LVLAB_INTEGRATION=1``. The default test run skips every
function in this module via the ``integration`` marker. See
``tests/conftest.py`` and the "Integration test storage layout"
section of ``docs/CONTRIBUTING.md`` for the storage and naming
conventions every test here must follow.

The manifest workflow has a wider blast radius than the standalone
``createvm`` / ``destroyvm`` scripts: lvlab namespaces its libvirt
domains as ``<vm_name>_<env_name>`` and places per-VM artifacts
under ``<disk_image_basedir>/<env_name>/<vm_name>/``. To keep the
session domain + storage reapers effective without making them
recurse, tests here MUST prefix BOTH ``vm_name`` and ``env_name``
with :data:`tests.conftest.LVLAB_TEST_PREFIX` so every libvirt
domain, top-level storage subdir, and per-VM subdir is
reaper-recognisable in isolation.

The cloud-image cache at ``/var/lib/libvirt/images/cloud-images/``
is intentionally shared with normal lvlab usage — forcing tests to
re-download a 432 MB qcow2 every run would make the suite hostile
to iterate. The test manifest sets ``cloud_image_basedir:
/var/lib/libvirt/images`` so lvlab's idempotent ``/cloud-images``
append resolves to that shared cache.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import (
    find_entry_point,
    list_domains,
    render_manifest,
    wait_for_no_domain,
)


_LVLAB_UP_TIMEOUT_SECONDS = 300
_LVLAB_STATUS_TIMEOUT_SECONDS = 30
_LVLAB_DESTROY_TIMEOUT_SECONDS = 60


@pytest.mark.integration
def test_lvlab_manifest_roundtrip(
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    tmp_path: Path,
) -> None:
    """``lvlab up`` defines a manifest VM; ``status`` lists it; ``destroy --force`` removes it.

    End-to-end exercises the manifest workflow against a real libvirt
    connection: real cloud-init render, real qcow2 backing-file disk,
    real cloud-init ISO, real virt-install, real undefine + on-disk
    cleanup via :class:`tkc_lvlab.utils.libvirt.Machine.destroy`.

    Both the libvirt domain (``<vm_name>_<env_name>``) and the
    on-disk storage subdir (``<storage_root>/<env_name>/<vm_name>/``)
    carry the per-session test prefix, so the session domain reaper
    and storage reaper will each recognise the artifacts independently
    even if the test crashes mid-sequence.

    Args:
        integration_uri: libvirt URI parametrized by the
            :func:`integration_uri` fixture (skipped per-URI if not
            test-ready).
        lvlab_integration_storage_root: libvirt-readable test storage
            root (``/var/lib/libvirt/images/lvlab-test/``). Becomes
            the manifest's ``disk_image_basedir``.
        tmp_path: pytest-managed scratch directory for the per-test
            ``Lvlab.yml`` file. lvlab reads the manifest from CWD, so
            the lvlab subprocess invocations use ``cwd=tmp_path``.
    """
    lvlab = find_entry_point("lvlab")

    pubkey_path = Path.home() / ".ssh" / "id_ed25519.pub"
    if not pubkey_path.is_file():
        pytest.skip(
            f"no SSH public key at {pubkey_path} — manifest's "
            f"cloud_init.pubkey requires a real key on disk"
        )

    # Distinguish parametrized URI runs so each gets its own per-VM
    # name and per-environment name (and thus distinct domain + storage
    # paths). ``make_test_name`` keeps the LVLAB_TEST_PREFIX on each
    # half so both the domain reaper and the storage reaper recognise
    # the resulting artifacts.
    uri_tag = "session" if "session" in integration_uri else "system"
    vm_name = make_test_name(f"vault.local-{uri_tag}")
    env_name = make_test_name(f"env-{uri_tag}")
    expected_domain = f"{vm_name}_{env_name}"
    assert_owned_by_test(expected_domain)

    manifest_path = tmp_path / "Lvlab.yml"
    manifest_path.write_text(
        render_manifest(
            env_name=env_name,
            uri=integration_uri,
            storage_root=lvlab_integration_storage_root,
            vm_name=vm_name,
            pubkey_path=pubkey_path,
        )
    )

    up_result = subprocess.run(
        [lvlab, "up", vm_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=_LVLAB_UP_TIMEOUT_SECONDS,
        cwd=tmp_path,
    )

    try:
        assert up_result.returncode == 0, (
            f"lvlab up failed (exit {up_result.returncode}):\n"
            f"stdout:\n{up_result.stdout}\n"
            f"stderr:\n{up_result.stderr}"
        )

        assert expected_domain in list_domains(integration_uri), (
            f"lvlab up reported success but domain {expected_domain!r} "
            f"is not in virsh list on {integration_uri}"
        )

        per_vm_dir = lvlab_integration_storage_root / env_name / vm_name
        assert per_vm_dir.is_dir(), (
            f"lvlab up reported success but storage dir {per_vm_dir} "
            f"was not created"
        )
        assert (per_vm_dir / "disk0.qcow2").is_file(), (
            f"lvlab up reported success but {per_vm_dir / 'disk0.qcow2'} " f"is missing"
        )
        assert (per_vm_dir / "cidata.iso").is_file(), (
            f"lvlab up reported success but {per_vm_dir / 'cidata.iso'} " f"is missing"
        )

        # `lvlab status` reads the manifest, queries libvirt for each
        # declared machine, and prints a per-machine line. Confirm it
        # exits 0 and includes the test VM in its output — the
        # per-machine line is what verifies the manifest+libvirt
        # consistency check works.
        status_result = subprocess.run(
            [lvlab, "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_STATUS_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert status_result.returncode == 0, (
            f"lvlab status failed (exit {status_result.returncode}):\n"
            f"stdout:\n{status_result.stdout}\n"
            f"stderr:\n{status_result.stderr}"
        )
        combined_status_output = status_result.stdout + status_result.stderr
        assert vm_name in combined_status_output, (
            f"lvlab status did not include the test VM {vm_name!r} "
            f"in its output:\n{combined_status_output}"
        )
    finally:
        # lvlab destroy --force must always run — even if the
        # assertions above failed, leave the host clean. The session
        # reapers are a safety net, not a substitute for explicit
        # cleanup.
        destroy_result = subprocess.run(
            [lvlab, "destroy", vm_name, "--force"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_DESTROY_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )

    assert destroy_result.returncode == 0, (
        f"lvlab destroy failed (exit {destroy_result.returncode}):\n"
        f"stdout:\n{destroy_result.stdout}\n"
        f"stderr:\n{destroy_result.stderr}"
    )

    wait_for_no_domain(integration_uri, expected_domain)
