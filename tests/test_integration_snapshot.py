"""Integration tests for the ``lvlab snapshot`` lifecycle.

Covers Phase 3 L178 sub-bullet: "snapshot create/list/delete against a
real domain". This file is separate from ``test_integration_lvlab.py``
because the lvlab round-trip test (up/status/destroy) exercises manifest
lifecycle, whereas snapshot create/list/delete is a distinct behavioural
concern — a domain must already exist before any snapshot operation is
meaningful, and the snapshot state machine (zero → one → zero) warrants
its own explicit sequence.

Gated by ``LVLAB_INTEGRATION=1``. The default ``uv run pytest`` run
skips everything here via the ``integration`` marker. See
``tests/conftest.py`` and the "Integration test storage layout" section
of ``docs/CONTRIBUTING.md`` for the naming and storage safety rules
every test here follows.

Both ``vm_name`` and ``env_name`` carry :data:`tests.conftest.LVLAB_TEST_PREFIX`
so the session domain reaper recognises the libvirt domain
(``<vm_name>_<env_name>``) and the session storage reaper recognises the
on-disk artifact directory — even if the test crashes mid-sequence.
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
_LVLAB_SNAPSHOT_TIMEOUT_SECONDS = 30
_LVLAB_DESTROY_TIMEOUT_SECONDS = 60

_SNAPSHOT_NAME = "baseline"
"""Stable snapshot name used throughout the lifecycle sequence.

The snapshot name itself does not carry LVLAB_TEST_PREFIX because it is
metadata stored inside the libvirt domain, which is itself fully
prefixed. The session domain reaper deletes the domain (and all its
snapshots) transitively when it runs ``virsh undefine --remove-all-storage``.
"""


@pytest.mark.integration
def test_snapshot_lifecycle(
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    tmp_path: Path,
) -> None:
    """``lvlab snapshot create/list/delete`` round-trip on a real domain.

    Sequence:
    1. ``lvlab up <vm_name>`` brings a manifest VM online.
    2. ``lvlab snapshot list <vm_name>`` reports zero snapshots.
    3. ``lvlab snapshot create <vm_name> baseline`` creates one snapshot.
    4. ``lvlab snapshot list <vm_name>`` lists exactly the one snapshot by name.
    5. ``lvlab snapshot delete <vm_name> baseline --force`` deletes it.
    6. ``lvlab snapshot list <vm_name>`` reports zero snapshots again.
    7. ``lvlab destroy <vm_name> --force`` in a ``finally`` block cleans up.

    ``snapshot delete`` uses ``--force`` (a ``typer.Option`` defined on
    the command at cli.py:524) to bypass the interactive confirmation
    prompt. This matches the ``lvlab destroy --force`` pattern already
    established in the codebase and avoids the need to pipe ``"y\n"``
    via ``subprocess.run(input=...)``.

    Args:
        integration_uri: libvirt URI parametrized by the
            :func:`integration_uri` fixture (skipped per-URI if not
            test-ready).
        lvlab_integration_storage_root: libvirt-readable test storage
            root (``/var/lib/libvirt/images/lvlab-test/``). Becomes
            the manifest's ``disk_image_basedir``.
        tmp_path: pytest-managed scratch directory for the per-test
            ``Lvlab.yml`` file. lvlab reads the manifest from CWD so
            all lvlab subprocess invocations use ``cwd=tmp_path``.
    """
    lvlab = find_entry_point("lvlab")

    pubkey_path = Path.home() / ".ssh" / "id_ed25519.pub"
    if not pubkey_path.is_file():
        pytest.skip(
            f"no SSH public key at {pubkey_path} — manifest's "
            f"cloud_init.pubkey requires a real key on disk"
        )

    # Distinguish parametrized URI runs so each gets its own per-VM name
    # and per-environment name. Both halves carry LVLAB_TEST_PREFIX so the
    # domain reaper (``<vm_name>_<env_name>``) and storage reaper
    # (``<storage_root>/<env_name>/<vm_name>/``) each recognise the
    # resulting artifacts independently.
    uri_tag = "session" if "session" in integration_uri else "system"
    vm_name = make_test_name(f"snap-vm-{uri_tag}")
    env_name = make_test_name(f"snap-env-{uri_tag}")
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

        # --- Step 2: snapshot list before any snapshot exists ---
        list_before_result = subprocess.run(
            [lvlab, "snapshot", "list", vm_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_SNAPSHOT_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert list_before_result.returncode == 0, (
            f"lvlab snapshot list failed before create "
            f"(exit {list_before_result.returncode}):\n"
            f"stdout:\n{list_before_result.stdout}\n"
            f"stderr:\n{list_before_result.stderr}"
        )
        combined_before = list_before_result.stdout + list_before_result.stderr
        assert "No snapshots found" in combined_before, (
            f"expected 'No snapshots found' in snapshot list output before "
            f"any snapshot was created, got:\n{combined_before}"
        )

        # --- Step 3: create a snapshot ---
        create_result = subprocess.run(
            [lvlab, "snapshot", "create", vm_name, _SNAPSHOT_NAME],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_SNAPSHOT_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert create_result.returncode == 0, (
            f"lvlab snapshot create failed "
            f"(exit {create_result.returncode}):\n"
            f"stdout:\n{create_result.stdout}\n"
            f"stderr:\n{create_result.stderr}"
        )

        # --- Step 4: snapshot list shows exactly the one snapshot ---
        list_after_create_result = subprocess.run(
            [lvlab, "snapshot", "list", vm_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_SNAPSHOT_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert list_after_create_result.returncode == 0, (
            f"lvlab snapshot list failed after create "
            f"(exit {list_after_create_result.returncode}):\n"
            f"stdout:\n{list_after_create_result.stdout}\n"
            f"stderr:\n{list_after_create_result.stderr}"
        )
        combined_after_create = (
            list_after_create_result.stdout + list_after_create_result.stderr
        )
        assert _SNAPSHOT_NAME in combined_after_create, (
            f"snapshot name {_SNAPSHOT_NAME!r} not found in snapshot list "
            f"output after create:\n{combined_after_create}"
        )

        # --- Step 5: delete the snapshot ---
        # --force bypasses the interactive confirmation prompt (defined at
        # cli.py:524 as ``typer.Option(False, "--force", ...)``) so no
        # stdin piping is needed. This mirrors the ``lvlab destroy --force``
        # convention already established throughout the integration suite.
        delete_result = subprocess.run(
            [lvlab, "snapshot", "delete", vm_name, _SNAPSHOT_NAME, "--force"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_SNAPSHOT_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert delete_result.returncode == 0, (
            f"lvlab snapshot delete failed "
            f"(exit {delete_result.returncode}):\n"
            f"stdout:\n{delete_result.stdout}\n"
            f"stderr:\n{delete_result.stderr}"
        )

        # --- Step 6: snapshot list is empty again ---
        list_after_delete_result = subprocess.run(
            [lvlab, "snapshot", "list", vm_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_SNAPSHOT_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert list_after_delete_result.returncode == 0, (
            f"lvlab snapshot list failed after delete "
            f"(exit {list_after_delete_result.returncode}):\n"
            f"stdout:\n{list_after_delete_result.stdout}\n"
            f"stderr:\n{list_after_delete_result.stderr}"
        )
        combined_after_delete = (
            list_after_delete_result.stdout + list_after_delete_result.stderr
        )
        assert "No snapshots found" in combined_after_delete, (
            f"expected 'No snapshots found' in snapshot list output after "
            f"delete, got:\n{combined_after_delete}"
        )

    finally:
        # lvlab destroy --force must always run — even if the assertions
        # above failed, leave the host clean. The session reapers are a
        # safety net, not a substitute for explicit cleanup.
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
