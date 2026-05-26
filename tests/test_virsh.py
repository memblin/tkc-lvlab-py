"""Unit tests for :mod:`tkc_lvlab.utils.virsh`.

These tests mock ``subprocess.run`` at the boundary; nothing here ever
actually invokes ``virsh``. They lock in the contract subsequent Phase 2
agents will build on.
"""

from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils import virsh
from tkc_lvlab.utils.virsh import (
    DEAD_STATES,
    DESTROYABLE_STATES,
    DOMSTATE_HUMAN,
    RUNNING_STATES,
    SHUTDOWNABLE_STATES,
    DomInfo,
    VirshError,
    _xml_tempfile,
    humanize_state,
    run_virsh,
    virsh_capabilities,
    virsh_dominfo,
    virsh_domstate,
    virsh_domstate_reason,
    virsh_list_all_names,
    virsh_snapshot_names,
)


URI = "qemu:///session"


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    """Build a CompletedProcess stub mirroring what subprocess.run returns."""
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# run_virsh — happy path
# ---------------------------------------------------------------------------


def test_run_virsh_builds_correct_argv_and_env():
    """argv is ``virsh -c <uri> <args...>`` and env forces LC_ALL=C + LANG=C."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed(stdout="ok\n")
    ) as run:
        result = run_virsh(URI, ["list", "--all", "--name"])

    assert result.stdout == "ok\n"
    run.assert_called_once()
    call_args, call_kwargs = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "list", "--all", "--name"]
    env = call_kwargs["env"]
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    # env should be derived from os.environ — keys present in os.environ are present here.
    for key in os.environ:
        assert key in env


def test_run_virsh_capture_true_sets_text_mode_and_pipes():
    """capture=True passes stdout/stderr pipes and text-mode encoding kwargs."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed()
    ) as run:
        run_virsh(URI, ["domstate", "foo"])

    _, kwargs = run.call_args
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == 30.0
    assert kwargs["input"] is None


def test_run_virsh_capture_false_does_not_pipe():
    """capture=False omits the stdout/stderr/text plumbing for passthrough."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed()
    ) as run:
        run_virsh(URI, ["console", "foo"], capture=False)

    _, kwargs = run.call_args
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    assert "text" not in kwargs


def test_run_virsh_custom_timeout_is_forwarded():
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed()
    ) as run:
        run_virsh(
            URI, ["snapshot-create", "vm", "--xmlfile", "/tmp/x.xml"], timeout=120.0
        )
    _, kwargs = run.call_args
    assert kwargs["timeout"] == 120.0


def test_run_virsh_input_text_is_forwarded():
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed()
    ) as run:
        run_virsh(URI, ["foo"], input_text="hello\n")
    _, kwargs = run.call_args
    assert kwargs["input"] == "hello\n"


# ---------------------------------------------------------------------------
# run_virsh — error paths
# ---------------------------------------------------------------------------


def test_run_virsh_nonzero_rc_raises_virsh_error():
    """Nonzero returncode raises VirshError carrying rc, stderr, and args."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(
            stderr="error: Failed to get domain 'nope'\n", returncode=1
        ),
    ):
        with pytest.raises(VirshError) as excinfo:
            run_virsh(URI, ["domstate", "nope"])

    err = excinfo.value
    assert err.returncode == 1
    assert err.stderr == "error: Failed to get domain 'nope'"
    # ``BaseException.args`` is always coerced to a tuple by the runtime,
    # regardless of what __init__ assigns. We compare as a sequence.
    assert tuple(err.args) == ("domstate", "nope")
    assert "domstate nope" in str(err)
    assert "rc=1" in str(err)
    assert "Failed to get domain" in str(err)


def test_run_virsh_check_false_returns_failed_result():
    """check=False suppresses the exception and returns the process object."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(stderr="boom", returncode=5),
    ):
        result = run_virsh(URI, ["foo"], check=False)
    assert result.returncode == 5
    assert result.stderr == "boom"


def test_run_virsh_file_not_found_raises_virsh_error_127():
    """Missing virsh binary surfaces as VirshError(127, documented-msg, ...)."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", side_effect=FileNotFoundError()
    ):
        with pytest.raises(VirshError) as excinfo:
            run_virsh(URI, ["list"])
    err = excinfo.value
    assert err.returncode == 127
    assert err.stderr == "virsh binary not found in PATH; install libvirt-clients"
    assert tuple(err.args) == ("list",)


def test_run_virsh_timeout_raises_virsh_error_minus_one():
    """Subprocess TimeoutExpired surfaces as VirshError(-1, timed-out-msg, ...)."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["virsh"], timeout=30.0),
    ):
        with pytest.raises(VirshError) as excinfo:
            run_virsh(URI, ["list", "--all"])
    err = excinfo.value
    assert err.returncode == -1
    assert "virsh timed out after 30.0s" in err.stderr
    assert "list --all" in err.stderr
    assert tuple(err.args) == ("list", "--all")


def test_virsh_error_empty_stderr_uses_placeholder():
    """An empty stderr still produces a readable str(err)."""
    err = VirshError(2, "", ["foo"])
    assert "<no stderr>" in str(err)
    assert err.stderr == ""
    assert tuple(err.args) == ("foo",)
    assert err.returncode == 2


# ---------------------------------------------------------------------------
# State strings & humanize_state
# ---------------------------------------------------------------------------


def test_state_sets_contents():
    """The four state-membership sets stay exactly as the design spec dictates."""
    assert RUNNING_STATES == {"running", "paused"}
    assert DEAD_STATES == {"shut off", "crashed"}
    assert SHUTDOWNABLE_STATES == {"running", "idle", "paused", "pmsuspended"}
    assert DESTROYABLE_STATES == {"running", "paused"}


@pytest.mark.parametrize(
    "state,expected",
    [
        ("no state", "no state"),
        ("running", "the machine is running"),
        ("idle", "the machine is blocked on resource"),
        ("paused", "the machine is paused by user"),
        ("in shutdown", "the machine is being shut down"),
        ("shut off", "the machine is shut off"),
        ("crashed", "the machine is crashed"),
        ("pmsuspended", "the machine is suspended by guest power management"),
    ],
)
def test_humanize_state_known_states(state, expected):
    """Every key in DOMSTATE_HUMAN maps to its documented human string."""
    status, _reason = humanize_state(state, "unknown")
    assert status == expected


def test_humanize_state_covers_every_domstate_key():
    """Sanity: the parametrized list above stays in sync with DOMSTATE_HUMAN."""
    parametrized = {
        "no state",
        "running",
        "idle",
        "paused",
        "in shutdown",
        "shut off",
        "crashed",
        "pmsuspended",
    }
    assert parametrized == set(DOMSTATE_HUMAN.keys())


def test_humanize_state_unknown_state_falls_back_to_raw():
    """An unknown state string is returned verbatim, no KeyError."""
    status, reason = humanize_state("hypothetical", "unknown")
    assert status == "hypothetical"
    # unknown reason for unknown state also falls back
    assert reason == "unknown"


def test_humanize_state_known_state_unknown_reason_falls_back():
    """Unknown reason falls back to the raw reason; no KeyError."""
    status, reason = humanize_state("running", "made-up-reason")
    assert status == "the machine is running"
    assert reason == "made-up-reason"


def test_humanize_state_known_state_known_reason_maps():
    """A known (state, reason) pair maps to the documented human string."""
    status, reason = humanize_state("running", "booted")
    assert status == "the machine is running"
    assert reason == "normal startup from boot"

    status, reason = humanize_state("shut off", "destroyed")
    assert status == "the machine is shut off"
    assert reason == "forced poweroff"


# ---------------------------------------------------------------------------
# _xml_tempfile
# ---------------------------------------------------------------------------


def test_xml_tempfile_writes_contents_and_cleans_up():
    """Body sees the file with the right contents; cleanup happens on exit."""
    payload = "<domainsnapshot><name>snap1</name></domainsnapshot>"
    seen_path = None
    with _xml_tempfile(payload) as path:
        seen_path = path
        assert os.path.exists(path)
        basename = os.path.basename(path)
        assert basename.startswith("lvlab-snapshot-")
        assert basename.endswith(".xml")
        with open(path, "r", encoding="utf-8") as fh:
            assert fh.read() == payload
    assert seen_path is not None
    assert not os.path.exists(seen_path)


def test_xml_tempfile_cleans_up_on_exception():
    """Cleanup also runs when the body raises."""
    seen_path = None
    with pytest.raises(RuntimeError, match="boom"):
        with _xml_tempfile("<x/>") as path:
            seen_path = path
            assert os.path.exists(path)
            raise RuntimeError("boom")
    assert seen_path is not None
    assert not os.path.exists(seen_path)


def test_xml_tempfile_handles_already_gone_file():
    """If the file disappears before cleanup, the context manager still exits cleanly."""
    seen_path: str | None = None
    with _xml_tempfile("<x/>") as path:
        seen_path = path
        # Simulate a racing unlink — file is gone before the finally-block
        # gets to it. The context manager must not raise.
        os.unlink(path)
        assert not os.path.exists(path)
    assert seen_path is not None
    assert not os.path.exists(seen_path)


def test_xml_tempfile_unlink_oserror_is_swallowed():
    """Cleanup never propagates OSError from a racing unlink."""
    seen_path: str | None = None
    with mock.patch(
        "tkc_lvlab.utils.virsh.os.unlink", side_effect=OSError("simulated race")
    ):
        with _xml_tempfile("<x/>") as path:
            seen_path = path
            assert os.path.exists(path)
        # Cleanup must not propagate the OSError. (If it did, this line
        # would never execute.)
    # Real cleanup never happened (we patched it out); clean it up ourselves
    # outside the mock so the real os.unlink is used.
    assert seen_path is not None
    if os.path.exists(seen_path):
        os.unlink(seen_path)


# ---------------------------------------------------------------------------
# Convenience parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("", []),
        ("\n", []),
        ("vm1\n", ["vm1"]),
        ("vm1\nvm2\nvm3\n", ["vm1", "vm2", "vm3"]),
        ("   vm1   \n  vm2\n", ["vm1", "vm2"]),
        ("vm1\n\nvm2\n", ["vm1", "vm2"]),
        # no trailing newline
        ("vm1\nvm2", ["vm1", "vm2"]),
    ],
)
def test_virsh_list_all_names_parses_one_per_line(stdout, expected):
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed(stdout=stdout)
    ) as run:
        names = virsh_list_all_names(URI)
    assert names == expected
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "list", "--all", "--name"]


def test_virsh_domstate_strips_and_lowercases():
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(stdout="  Running  \n"),
    ) as run:
        state = virsh_domstate(URI, "vm1")
    assert state == "running"
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "domstate", "vm1"]


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("running (booted)\n", ("running", "booted")),
        ("shut off (destroyed)\n", ("shut off", "destroyed")),
        ("paused (user)\n", ("paused", "user")),
        # virsh sometimes reports multi-word reasons
        ("running (migration canceled)\n", ("running", "migration canceled")),
        # Empty output → empty pair (defensive)
        ("\n", ("", "")),
        # Output without parens → state only
        ("shut off\n", ("shut off", "")),
    ],
)
def test_virsh_domstate_reason_parses(stdout, expected):
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed(stdout=stdout)
    ) as run:
        result = virsh_domstate_reason(URI, "vm1")
    assert result == expected
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "domstate", "--reason", "vm1"]


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("", []),
        ("snap1\n", ["snap1"]),
        ("snap1\nsnap2\nsnap3\n", ["snap1", "snap2", "snap3"]),
        ("   snap1\n  snap2  \n", ["snap1", "snap2"]),
    ],
)
def test_virsh_snapshot_names_parses(stdout, expected):
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed(stdout=stdout)
    ) as run:
        names = virsh_snapshot_names(URI, "vm1")
    assert names == expected
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "snapshot-list", "vm1", "--name"]


def test_virsh_capabilities_returns_raw_stdout():
    xml = "<capabilities>\n  <host/>\n</capabilities>\n"
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run", return_value=_completed(stdout=xml)
    ) as run:
        result = virsh_capabilities(URI)
    assert result == xml
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "capabilities"]


# ---------------------------------------------------------------------------
# virsh_dominfo — parse a real ``virsh dominfo`` capture.
# ---------------------------------------------------------------------------

# Captured from ``LC_ALL=C virsh dominfo <name>`` (libvirt 12.x). The field
# labels and the ``<n> KiB`` / ``enable`` / ``disable`` / ``yes`` / ``no``
# value vocabulary are what the parser keys off — do not hand-edit into a
# fabricated round-trip shape.
DOMINFO_RUNNING_SAMPLE = """\
Id:             3
Name:           web01_demo
UUID:           4dea22b3-1d52-d8f3-2516-782e98ab3fa0
OS Type:        hvm
State:          running
CPU(s):         2
CPU time:       126.7s
Max memory:     2097152 KiB
Used memory:    2097152 KiB
Persistent:     yes
Autostart:      enable
Managed save:   no
Security model: none
Security DOI:   0
"""

DOMINFO_SHUTOFF_SAMPLE = """\
Id:             -
Name:           build02_demo
UUID:           9f1c8b2a-5e44-4c10-9a77-0b2d6f3e1c55
OS Type:        hvm
State:          shut off
CPU(s):         1
Max memory:     1048576 KiB
Used memory:    1048576 KiB
Persistent:     yes
Autostart:      disable
Managed save:   no
Security model: none
Security DOI:   0
"""


def test_virsh_dominfo_parses_running_sample():
    """A running domain's dominfo parses into the cheap typed fields."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(stdout=DOMINFO_RUNNING_SAMPLE),
    ) as run:
        info = virsh_dominfo(URI, "web01_demo")

    assert isinstance(info, DomInfo)
    assert info.state == "running"
    assert info.vcpus == 2
    assert info.max_memory_kib == 2097152
    assert info.autostart is True
    assert info.persistent is True
    # Exactly one virsh call, and it is a plain dominfo (cheap-read contract).
    call_args, _ = run.call_args
    assert call_args[0] == ["virsh", "-c", URI, "dominfo", "web01_demo"]
    assert run.call_count == 1


def test_virsh_dominfo_parses_shutoff_sample():
    """A shut-off, non-autostart domain parses autostart=False, state preserved."""
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(stdout=DOMINFO_SHUTOFF_SAMPLE),
    ):
        info = virsh_dominfo(URI, "build02_demo")

    assert info.state == "shut off"
    assert info.vcpus == 1
    assert info.max_memory_kib == 1048576
    assert info.autostart is False
    assert info.persistent is True


def test_virsh_dominfo_propagates_virsh_error_for_missing_domain():
    """A nonzero exit (domain not found) surfaces as VirshError for the caller to skip."""
    err = VirshError(1, "error: failed to get domain 'nope'", ["dominfo", "nope"])
    with mock.patch("tkc_lvlab.utils.virsh.run_virsh", side_effect=err):
        with pytest.raises(VirshError):
            virsh_dominfo(URI, "nope")


def test_virsh_dominfo_missing_numeric_lines_yield_none():
    """If CPU(s)/Max memory lines are absent, the int fields are None, not 0/crash."""
    sparse = "Name:           x\nState:          paused\nPersistent:     no\n"
    with mock.patch(
        "tkc_lvlab.utils.virsh.subprocess.run",
        return_value=_completed(stdout=sparse),
    ):
        info = virsh_dominfo(URI, "x")

    assert info.state == "paused"
    assert info.vcpus is None
    assert info.max_memory_kib is None
    assert info.autostart is False
    assert info.persistent is False


# ---------------------------------------------------------------------------
# Module-level re-exports — smoke check so future refactors don't quietly
# rename the public constants used by callers.
# ---------------------------------------------------------------------------


def test_module_exports_public_names():
    for name in [
        "run_virsh",
        "VirshError",
        "DOMSTATE_HUMAN",
        "RUNNING_STATES",
        "DEAD_STATES",
        "SHUTDOWNABLE_STATES",
        "DESTROYABLE_STATES",
        "humanize_state",
        "virsh_list_all_names",
        "virsh_domstate",
        "virsh_domstate_reason",
        "virsh_dominfo",
        "DomInfo",
        "virsh_snapshot_names",
        "virsh_capabilities",
    ]:
        assert hasattr(virsh, name), f"virsh module missing public name {name!r}"
