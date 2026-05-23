"""Unit tests for :class:`tkc_lvlab.utils.libvirt.Machine` methods that have
been ported to ``virsh`` (Phase 2).

These tests patch the ``virsh_*`` collaborators at the
``tkc_lvlab.utils.libvirt`` import boundary so nothing here actually shells
out. The ``Machine`` object is constructed without running ``__init__`` —
the methods under test depend only on ``libvirt_vm_name``, and the real
constructor has unrelated filesystem side effects.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tkc_lvlab.utils.libvirt import Machine
from tkc_lvlab.utils.virsh import VirshError


@pytest.fixture
def machine() -> Machine:
    """A Machine stub whose only populated attribute is libvirt_vm_name."""
    m = object.__new__(Machine)
    m.libvirt_vm_name = "web01_lab"
    m.vm_name = "web01"
    return m


URI = "qemu:///session"


# ---------------------------------------------------------------------------
# exists_in_libvirt — return-shape and lookup behavior
# ---------------------------------------------------------------------------


def test_exists_in_libvirt_absent_returns_empty_strings(machine: Machine) -> None:
    """When the domain isn't in the list, return (False, "", "") — not the
    old (False, 0, 0) tuple — and don't call domstate at all."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names", return_value=["other_lab"]
        ) as list_mock,
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate_reason") as state_mock,
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (False, "", "")
    list_mock.assert_called_once_with(URI)
    state_mock.assert_not_called()


def test_exists_in_libvirt_present_returns_state_and_reason(machine: Machine) -> None:
    """When the domain is present, surface the lowercase virsh state strings
    cli.py now compares against (``running``, ``shut off``, etc.)."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab", "other_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_domstate_reason",
            return_value=("running", "booted"),
        ) as state_mock,
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (True, "running", "booted")
    state_mock.assert_called_once_with(URI, "web01_lab")


def test_exists_in_libvirt_namespacing_uses_libvirt_vm_name(machine: Machine) -> None:
    """The lookup must use the env-namespaced name, not the bare vm_name.
    Regression guard: ``machines[].vm_name`` of ``web01`` in two environments
    must not collide; only ``web01_<env>`` is the real domain name."""
    machine.libvirt_vm_name = "web01_prod"
    with mock.patch(
        "tkc_lvlab.utils.libvirt.virsh_list_all_names",
        return_value=["web01_dev"],  # the dev namespaced name, not prod
    ):
        exists, _, _ = machine.exists_in_libvirt(URI)
    assert exists is False


def test_exists_in_libvirt_domstate_race_treated_as_absent(machine: Machine) -> None:
    """If the domain vanishes between the list and the lookup, ``virsh``
    raises ``VirshError`` for the second call. The method should treat that
    as 'no longer present' rather than crashing the caller."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names", return_value=["web01_lab"]
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_domstate_reason",
            side_effect=VirshError(1, "domain not found", ["domstate"]),
        ),
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (False, "", "")


def test_exists_in_libvirt_list_failure_propagates(machine: Machine) -> None:
    """A failure of the initial ``virsh list`` (URI unreachable, virsh
    missing, etc.) is an environmental problem — surface it, don't swallow."""
    with mock.patch(
        "tkc_lvlab.utils.libvirt.virsh_list_all_names",
        side_effect=VirshError(127, "virsh not found", ["list"]),
    ):
        with pytest.raises(VirshError):
            machine.exists_in_libvirt(URI)


# ---------------------------------------------------------------------------
# __init__ — shared_directories source-path expansion
# ---------------------------------------------------------------------------


def test_shared_directories_source_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``~/path`` in a manifest's shared_directories source gets expanded
    against the user's $HOME so the same manifest works across machines."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_defaults = {
        "shared_directories": [
            {"source": "~/repos", "mount_tag": "gitrepos"},
        ],
        "interfaces": {"nameservers": {}},
    }
    environment = {"name": "lab"}
    machine_cfg = {"vm_name": "web01"}

    m = Machine(machine_cfg, environment, config_defaults)

    assert m.shared_directories == [
        {"source": str(tmp_path / "repos"), "mount_tag": "gitrepos"},
    ]


def test_shared_directories_source_expands_envvar(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``$VAR``-style references in shared_directories source are also
    expanded — matches the behavior of disk_image_basedir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LVLAB_TEST_SRC", str(tmp_path / "custom"))
    config_defaults = {
        "shared_directories": [
            {"source": "$LVLAB_TEST_SRC", "mount_tag": "custom"},
        ],
        "interfaces": {"nameservers": {}},
    }
    environment = {"name": "lab"}
    machine_cfg = {"vm_name": "web01"}

    m = Machine(machine_cfg, environment, config_defaults)

    assert m.shared_directories[0]["source"] == str(tmp_path / "custom")
