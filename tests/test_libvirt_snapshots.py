"""Unit tests for :class:`tkc_lvlab.utils.libvirt.Machine` snapshot methods
ported to ``virsh`` (Phase 2 step 3B).

These tests patch the ``virsh_*`` collaborators at the
``tkc_lvlab.utils.libvirt`` import boundary so nothing here actually shells
out. The ``Machine`` object is constructed without running ``__init__`` —
the methods under test depend only on ``libvirt_vm_name`` (and ``vm_name``
for the "absent" warning log), and the real constructor has unrelated
filesystem side effects.
"""

from __future__ import annotations

import contextlib
from unittest import mock

import pytest

from tkc_lvlab.utils.libvirt import Machine
from tkc_lvlab.utils.virsh import VirshError

URI = "qemu:///session"


@pytest.fixture
def machine() -> Machine:
    """A Machine stub whose only populated attributes are the two names the
    snapshot methods read."""
    m = object.__new__(Machine)
    m.libvirt_vm_name = "web01_lab"
    m.vm_name = "web01"
    return m


@contextlib.contextmanager
def _fake_xml_tempfile(xml: str):
    """Mock replacement for ``_xml_tempfile`` that records the XML and yields
    a predictable path. The real helper is itself unit-tested in
    ``test_virsh.py``; here we only care that the right XML reached it and
    the right path reached ``run_virsh``."""
    _fake_xml_tempfile.captured_xml = xml
    yield "/tmp/lvlab-snapshot-FAKE.xml"


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------


def test_list_snapshots_present_with_snapshots_preserves_order(
    machine: Machine,
) -> None:
    """When the domain exists, return the names ``virsh snapshot-list --name``
    emits, in order. Order matters because callers display them to a human
    and the libvirt-python implementation preserved creation order."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab", "other_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_snapshot_names",
            return_value=["snap-a", "snap-b", "snap-c"],
        ) as snap_mock,
    ):
        result = machine.list_snapshots(URI)

    assert result == ["snap-a", "snap-b", "snap-c"]
    snap_mock.assert_called_once_with(URI, "web01_lab")


def test_list_snapshots_present_no_snapshots_returns_empty(machine: Machine) -> None:
    """A defined domain with zero snapshots: ``[]``, not ``None`` and not a
    crash. Regression guard: a wrong-shape return here breaks the for-loop
    in the cli.py snapshot command."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_snapshot_names",
            return_value=[],
        ),
    ):
        assert machine.list_snapshots(URI) == []


def test_list_snapshots_absent_domain_returns_empty(machine: Machine) -> None:
    """If the domain isn't defined at the URI, return ``[]`` and don't
    call ``virsh snapshot-list``. This mirrors the previous libvirt-python
    behavior (the conn was opened but no lookup happened)."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_snapshot_names") as snap_mock,
    ):
        assert machine.list_snapshots(URI) == []

    snap_mock.assert_not_called()


def test_list_snapshots_race_treated_as_absent(machine: Machine) -> None:
    """If the domain disappears between the list and the snapshot-list
    call, treat that as 'no snapshots' rather than crashing. The previous
    libvirt-python code would have raised; the port should not regress to
    that behavior."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_snapshot_names",
            side_effect=VirshError(1, "domain not found", ["snapshot-list"]),
        ),
    ):
        assert machine.list_snapshots(URI) == []


# ---------------------------------------------------------------------------
# create_snapshot
# ---------------------------------------------------------------------------


def test_create_snapshot_happy_path_uses_xmlfile_and_timeout(
    machine: Machine,
) -> None:
    """Default-description path: writes XML via ``_xml_tempfile``, calls
    ``virsh snapshot-create --xmlfile <path>`` with ``timeout=120.0``, and
    returns ``True``. The XML must carry the snapshot name and the
    auto-generated description ``Snapshot of <libvirt_vm_name>`` so a
    reviewer scanning libvirt later can see what each snapshot was for."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt._xml_tempfile",
            side_effect=_fake_xml_tempfile,
        ),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.create_snapshot(URI, "snap-1")

    assert result is True

    captured_xml = _fake_xml_tempfile.captured_xml
    assert "<name>snap-1</name>" in captured_xml
    # Default-description branch — must use libvirt_vm_name, not vm_name.
    assert "<description>Snapshot of web01_lab</description>" in captured_xml
    assert "<domainsnapshot>" in captured_xml

    run_mock.assert_called_once_with(
        URI,
        ["snapshot-create", "web01_lab", "--xmlfile", "/tmp/lvlab-snapshot-FAKE.xml"],
        timeout=120.0,
    )


def test_create_snapshot_custom_description(machine: Machine) -> None:
    """When the caller supplies a description, that string lands in the
    rendered XML verbatim — not the default-fallback text."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt._xml_tempfile",
            side_effect=_fake_xml_tempfile,
        ),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh"),
    ):
        machine.create_snapshot(URI, "snap-2", "pre-upgrade baseline")

    captured_xml = _fake_xml_tempfile.captured_xml
    assert "<name>snap-2</name>" in captured_xml
    assert "<description>pre-upgrade baseline</description>" in captured_xml
    # No leak of the default text.
    assert "Snapshot of web01_lab" not in captured_xml


def test_create_snapshot_propagates_virsh_error(machine: Machine) -> None:
    """A ``virsh snapshot-create`` failure must propagate ``VirshError``.
    Regression guard for the historical ``return inside finally`` bug that
    silently turned exceptions into return values — see Phase 2 design
    §6.7."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt._xml_tempfile",
            side_effect=_fake_xml_tempfile,
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.run_virsh",
            side_effect=VirshError(1, "snapshot already exists", ["snapshot-create"]),
        ),
    ):
        with pytest.raises(VirshError):
            machine.create_snapshot(URI, "snap-dup")


def test_create_snapshot_absent_domain_raises(machine: Machine) -> None:
    """The previous implementation silently returned ``0`` (= "success") when
    the domain wasn't defined, which is misleading. The port raises so the
    caller gets a clean failure signal."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt._xml_tempfile") as tempfile_mock,
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        with pytest.raises(VirshError):
            machine.create_snapshot(URI, "snap-1")

    tempfile_mock.assert_not_called()
    run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# delete_snapshot
# ---------------------------------------------------------------------------


def test_delete_snapshot_happy_path_returns_none(machine: Machine) -> None:
    """Build the right argv (``snapshot-delete <name> <snap>``), pass
    ``timeout=120.0``, and return ``None`` on success."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.delete_snapshot(URI, "snap-1")

    assert result is None
    run_mock.assert_called_once_with(
        URI,
        ["snapshot-delete", "web01_lab", "snap-1"],
        timeout=120.0,
    )


def test_delete_snapshot_propagates_virsh_error(machine: Machine) -> None:
    """A ``virsh snapshot-delete`` failure (e.g. snapshot doesn't exist)
    must propagate. Regression guard for the historical ``return inside
    finally`` bug — see Phase 2 design §6.7."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.run_virsh",
            side_effect=VirshError(1, "snapshot not found", ["snapshot-delete"]),
        ),
    ):
        with pytest.raises(VirshError):
            machine.delete_snapshot(URI, "missing-snap")


def test_delete_snapshot_absent_domain_raises(machine: Machine) -> None:
    """The previous implementation silently returned ``0`` when the domain
    wasn't defined. The port raises so the caller knows nothing got
    deleted — destructive paths must never lie about success."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        with pytest.raises(VirshError):
            machine.delete_snapshot(URI, "snap-1")

    run_mock.assert_not_called()
