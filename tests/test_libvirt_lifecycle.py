"""Unit tests for :class:`tkc_lvlab.utils.libvirt.Machine` lifecycle methods
(``destroy``, ``poweron``, ``shutdown``) after the Phase 2 port to ``virsh``.

These tests patch the ``virsh_*`` collaborators at the
``tkc_lvlab.utils.libvirt`` import boundary so nothing here actually shells
out to ``virsh``. The ``Machine`` object is constructed without running
``__init__`` — the methods under test depend only on ``libvirt_vm_name``,
``vm_name``, and ``config_fpath``; the real constructor has unrelated
filesystem side effects.

The tests are scoped to behaviors that *could realistically break* during
the port:

* Lifecycle methods sequence their ``virsh`` calls correctly (e.g. destroy
  before undefine, snapshots deleted before undefine).
* The bool/int return contracts are preserved exactly, because ``cli.py``
  checks them with ``> 0`` and ``if machine.destroy(...)``.
* ``VirshError`` from any underlying call short-circuits the rest of the
  flow rather than tearing down later state on a half-failed machine.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tkc_lvlab.utils.libvirt import Machine
from tkc_lvlab.utils.virsh import VirshError


URI = "qemu:///session"


@pytest.fixture
def machine(tmp_path) -> Machine:
    """A Machine stub with the attributes lifecycle methods touch.

    ``config_fpath`` points at a real (empty) temp directory so ``destroy``'s
    file-cleanup block can exercise the happy path without writing to a real
    libvirt images directory.
    """
    m = object.__new__(Machine)
    m.libvirt_vm_name = "web01_lab"
    m.vm_name = "web01"
    m.config_fpath = str(tmp_path)
    return m


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def test_destroy_running_vm_calls_destroy_then_undefine(machine: Machine) -> None:
    """A running domain gets ``virsh destroy`` first, then (no snapshots)
    ``virsh undefine``. Ordering matters: undefining a running domain fails
    on real libvirt, so the destroy must come first.

    The undefine routes through ``undefine_with_snapshot_cleanup`` (whose
    own ``run_virsh`` lives in the snapshot_cleanup module), so both
    modules' ``run_virsh`` are funneled into one mock to capture ordering.
    """
    run_mock = mock.Mock()
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_domstate",
            side_effect=["running", "shut off"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh", run_mock),
        mock.patch("tkc_lvlab.utils.snapshot_cleanup.run_virsh", run_mock),
    ):
        result = machine.destroy(URI)

    assert result is True
    # Order: destroy, then undefine. No --snapshots-metadata since the plain
    # undefine succeeds (no snapshots).
    calls = [call.args[1] for call in run_mock.call_args_list]
    assert calls == [["destroy", "web01_lab"], ["undefine", "web01_lab"]]


def test_destroy_already_shut_off_skips_destroy(machine: Machine) -> None:
    """A domain that is already ``shut off`` must NOT have ``virsh destroy``
    invoked against it — that's an error on a stopped domain. The method
    should go straight to undefine."""
    run_mock = mock.Mock()
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh", run_mock),
        mock.patch("tkc_lvlab.utils.snapshot_cleanup.run_virsh", run_mock),
    ):
        result = machine.destroy(URI)

    assert result is True
    calls = [call.args[1] for call in run_mock.call_args_list]
    assert calls == [["undefine", "web01_lab"]]


def test_destroy_undefine_drops_snapshots_in_one_shot(
    machine: Machine,
) -> None:
    """``Machine.destroy`` routes undefine through the one-shot teardown
    (issue #96): a snapshot-blocked undefine retries with
    ``--snapshots-metadata`` rather than looping ``snapshot-delete``. The
    undefine still comes after the force-off."""
    run_mock = mock.Mock(
        side_effect=lambda uri, args, **kw: (
            _raise_snapshot_block()
            if args == ["undefine", "web01_lab"]
            else mock.DEFAULT
        )
    )
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh", run_mock),
        mock.patch("tkc_lvlab.utils.snapshot_cleanup.run_virsh", run_mock),
    ):
        result = machine.destroy(URI)

    assert result is True
    calls = [call.args[1] for call in run_mock.call_args_list]
    # Plain undefine (snapshot-blocked) -> retry with --snapshots-metadata.
    assert calls == [
        ["undefine", "web01_lab"],
        ["undefine", "web01_lab", "--snapshots-metadata"],
    ]


def _raise_snapshot_block():
    """Raise the snapshot-blocked undefine VirshError (test helper)."""
    from tkc_lvlab.utils.virsh import VirshError

    raise VirshError(
        1,
        "error: cannot delete inactive domain with 2 snapshots",
        ["undefine"],
    )


def test_destroy_absent_domain_returns_false(machine: Machine) -> None:
    """If ``virsh list`` doesn't show the domain, destroy is a no-op that
    returns False. It must NOT call destroy/undefine on a domain that's
    not there — that would be a wrong-target risk."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate") as state_mock,
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.destroy(URI)

    assert result is False
    state_mock.assert_not_called()
    run_mock.assert_not_called()


def test_destroy_destroy_failure_skips_undefine(machine: Machine) -> None:
    """If ``virsh destroy`` raises, we must NOT proceed to ``virsh undefine``
    or to file cleanup — that would leave behind a half-broken VM with no
    files. The whole operation returns False so the caller can react."""

    def fake_run(uri, args, **kwargs):
        if args[0] == "destroy":
            raise VirshError(1, "operation failed", args)
        return mock.MagicMock(returncode=0)

    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="running"),
        mock.patch(
            "tkc_lvlab.utils.libvirt.run_virsh", side_effect=fake_run
        ) as run_mock,
        mock.patch("tkc_lvlab.utils.snapshot_cleanup.run_virsh", side_effect=fake_run),
    ):
        result = machine.destroy(URI)

    assert result is False
    called_subcommands = [call.args[1][0] for call in run_mock.call_args_list]
    assert called_subcommands == ["destroy"]
    assert "undefine" not in called_subcommands


def test_destroy_undefine_failure_skips_file_cleanup(
    machine: Machine, tmp_path
) -> None:
    """If ``virsh undefine`` raises, on-disk files must NOT be removed.
    File removal in that case would leave libvirt with a half-defined
    domain pointing at deleted qcow2s — worse than leaving the files."""
    # Drop a sentinel file so we can prove we didn't touch it.
    sentinel = tmp_path / "disk0.qcow2"
    sentinel.write_text("not really a disk")

    def fake_run(uri, args, **kwargs):
        if args[0] == "undefine":
            raise VirshError(1, "operation failed", args)
        return mock.MagicMock(returncode=0)

    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh", side_effect=fake_run),
        mock.patch("tkc_lvlab.utils.snapshot_cleanup.run_virsh", side_effect=fake_run),
    ):
        result = machine.destroy(URI)

    assert result is False
    assert sentinel.exists(), "file cleanup must not run when undefine failed"


# (The former ``test_destroy_snapshot_cleanup_failure_skips_undefine`` is gone:
# issue #96 folded snapshot teardown into the one-shot undefine, so there is no
# longer a separate pre-undefine snapshot step that can fail independently. A
# failing one-shot undefine is covered by the undefine-failure test above.)


# ---------------------------------------------------------------------------
# poweron
# ---------------------------------------------------------------------------


def test_poweron_shut_off_vm_invokes_start(machine: Machine) -> None:
    """A ``shut off`` domain is the normal start case. Must call
    ``virsh start`` and return 0 to satisfy the ``> 0`` check in cli.py."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.poweron(URI)

    assert result == 0
    run_mock.assert_called_once_with(URI, ["start", "web01_lab"])


def test_poweron_crashed_vm_invokes_start(machine: Machine) -> None:
    """Per DEAD_STATES, a ``crashed`` domain is also startable — guard
    against accidentally narrowing the trigger set during the port."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="crashed"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.poweron(URI)

    assert result == 0
    run_mock.assert_called_once_with(URI, ["start", "web01_lab"])


def test_poweron_running_vm_is_noop(machine: Machine) -> None:
    """Already-running domains must not be ``virsh start``-ed again —
    that errors. The method must return 0 (not 1) so cli.py treats it as
    success."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="running"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.poweron(URI)

    assert result == 0
    run_mock.assert_not_called()


def test_poweron_start_failure_returns_one(machine: Machine) -> None:
    """A ``virsh start`` failure must surface as the ``> 0`` signal the
    cli.py caller looks for."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch(
            "tkc_lvlab.utils.libvirt.run_virsh",
            side_effect=VirshError(1, "boot failed", ["start", "web01_lab"]),
        ),
    ):
        result = machine.poweron(URI)

    assert result == 1


def test_poweron_absent_domain_returns_zero(machine: Machine) -> None:
    """Preserve the pre-port behavior: if the domain isn't defined, warn
    and return 0. ``cli.py`` does its own existence check before calling
    poweron, so this branch is the safety net, not the primary path."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate") as state_mock,
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.poweron(URI)

    assert result == 0
    state_mock.assert_not_called()
    run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["running", "idle", "paused", "pmsuspended"])
def test_shutdown_active_states_invoke_shutdown(machine: Machine, state: str) -> None:
    """Every state in SHUTDOWNABLE_STATES must trigger ``virsh shutdown``.
    Notable: virsh emits ``idle`` for what libvirt-python called
    ``VIR_DOMAIN_BLOCKED``; this parametrize protects against accidentally
    losing the ``idle`` case during the port."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value=state),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.shutdown(URI)

    assert result == 0
    run_mock.assert_called_once_with(URI, ["shutdown", "web01_lab"])


def test_shutdown_shut_off_vm_is_noop(machine: Machine) -> None:
    """Don't ``virsh shutdown`` a domain that's already off — virsh errors
    on that. Return 0 so cli.py treats the no-op as success."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="shut off"),
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.shutdown(URI)

    assert result == 0
    run_mock.assert_not_called()


def test_shutdown_failure_returns_one(machine: Machine) -> None:
    """A ``virsh shutdown`` failure must surface as the ``> 0`` signal
    the cli.py caller checks."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate", return_value="running"),
        mock.patch(
            "tkc_lvlab.utils.libvirt.run_virsh",
            side_effect=VirshError(1, "agent unresponsive", ["shutdown", "web01_lab"]),
        ),
    ):
        result = machine.shutdown(URI)

    assert result == 1


def test_shutdown_absent_domain_returns_zero(machine: Machine) -> None:
    """A missing domain is a no-op (return 0), matching the pre-port
    behavior. cli.py already checks existence, so this is defensive."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["other_lab"],
        ),
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate") as state_mock,
        mock.patch("tkc_lvlab.utils.libvirt.run_virsh") as run_mock,
    ):
        result = machine.shutdown(URI)

    assert result == 0
    state_mock.assert_not_called()
    run_mock.assert_not_called()
