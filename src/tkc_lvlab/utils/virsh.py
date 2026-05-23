"""Thin wrapper around the ``virsh`` CLI.

This module is the foundation for replacing the ``libvirt-python`` C-extension
dependency with subprocess calls to ``virsh``. Callers in
``tkc_lvlab/utils/libvirt.py`` and ``tkc_lvlab/cli.py`` will be ported to use
the helpers in this module in subsequent Phase 2 steps.

All helpers force the ``virsh`` locale to ``C`` so output parsing is stable
across host locales.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from typing import Iterator


class VirshError(RuntimeError):
    """Raised when a ``virsh`` invocation fails.

    Attributes:
        returncode: Exit code from ``virsh`` (or ``-1`` for timeouts, ``127``
            when the binary is missing).
        stderr: Captured stderr text (stripped). Empty if not available.
        args: The argument list passed to ``virsh`` (without the leading
            ``virsh -c <uri>``).
    """

    def __init__(self, returncode: int, stderr: str, args: list[str]):
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        # NB: ``BaseException.__init__`` would overwrite ``self.args`` with
        # the tuple of positional args passed to it. We want ``self.args`` to
        # expose the failing virsh argv (per Phase 2 design §1), so we
        # bypass super().__init__'s args-handling by passing nothing and then
        # storing our own value. ``__str__`` is overridden to build the
        # formatted message lazily.
        super().__init__()
        self.args = list(args)

    def __str__(self) -> str:
        return (
            f"virsh {' '.join(self.args)} failed (rc={self.returncode}): "
            f"{self.stderr or '<no stderr>'}"
        )


def run_virsh(
    uri: str,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = 30.0,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``virsh -c <uri> <args...>`` and return the completed process.

    The locale is forced to ``C`` via both ``LC_ALL`` and ``LANG``; some
    distros' ``virsh`` honors ``LANG`` even when ``LC_ALL`` is set, so both
    overrides are required to guarantee stable output parsing.

    Args:
        uri: libvirt connection URI (e.g. ``qemu:///session``).
        args: ``virsh`` subcommand and flags (no leading ``virsh -c <uri>``).
        check: If ``True``, raise :class:`VirshError` on nonzero exit.
        capture: If ``True``, capture stdout/stderr as text. If ``False``,
            inherit the parent's stdio (used for interactive subcommands).
        timeout: Wall-clock seconds before raising :class:`VirshError`. The
            default is 30s; long-running ops (snapshots) should pass a larger
            value explicitly.
        input_text: Optional stdin text. Reserved for future interactive use;
            no current Phase 2 caller passes this.

    Returns:
        The :class:`subprocess.CompletedProcess` from the invocation.

    Raises:
        VirshError: On nonzero exit (when ``check=True``), on missing
            ``virsh`` binary, or on timeout.
    """
    argv = ["virsh", "-c", uri, *args]
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"

    run_kwargs: dict = {
        "env": env,
        "timeout": timeout,
        "input": input_text,
    }
    if capture:
        run_kwargs.update(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    try:
        result = subprocess.run(
            argv, **run_kwargs
        )  # noqa: S603 (args are constructed in-process)
    except FileNotFoundError as exc:
        raise VirshError(
            127,
            "virsh binary not found in PATH; install libvirt-clients",
            args,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VirshError(
            -1,
            f"virsh timed out after {timeout}s: {' '.join(args)}",
            args,
        ) from exc

    if check and result.returncode != 0:
        stderr = result.stderr if capture else ""
        raise VirshError(result.returncode, stderr or "", args)

    return result


# ---------------------------------------------------------------------------
# State strings — what ``virsh domstate`` actually emits.
# ---------------------------------------------------------------------------

DOMSTATE_HUMAN: dict[str, str] = {
    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainState
    "no state": "no state",
    "running": "the machine is running",
    "idle": "the machine is blocked on resource",
    "paused": "the machine is paused by user",
    "in shutdown": "the machine is being shut down",
    "shut off": "the machine is shut off",
    "crashed": "the machine is crashed",
    "pmsuspended": "the machine is suspended by guest power management",
}

RUNNING_STATES: set[str] = {"running", "paused"}
DEAD_STATES: set[str] = {"shut off", "crashed"}
SHUTDOWNABLE_STATES: set[str] = {"running", "idle", "paused", "pmsuspended"}
DESTROYABLE_STATES: set[str] = {"running", "paused"}

# Reason map keyed on ``(state, reason)`` as ``virsh domstate --reason`` emits
# them. Missing keys fall back to the raw reason string.
#
# ``virsh`` emits the reason as a lowercase short word (e.g. ``booted``,
# ``user``, ``destroyed``) — the same suffix that libvirt's
# ``VIR_DOMAIN_<STATE>_<REASON>`` constants use. The values mirror the human
# strings the previous ``_humanize_machine_status`` returned so user-facing
# output is preserved across the port.
_REASON_HUMAN: dict[tuple[str, str], str] = {
    # virDomainRunningReason
    ("running", "unknown"): "Unknown",
    ("running", "booted"): "normal startup from boot",
    ("running", "migrated"): "migrated from another host",
    ("running", "restored"): "restored from a state file",
    ("running", "from snapshot"): "restored from snapshot",
    ("running", "unpaused"): "returned from paused state",
    ("running", "migration canceled"): "returned from migration",
    ("running", "save canceled"): "returned from failed save process",
    ("running", "wakeup"): "returned from pmsuspended due to wakeup event",
    ("running", "crashed"): "resumed from crashed",
    ("running", "post-copy"): "running in post-copy migration mode",
    ("running", "post-copy failed"): "running in failed post-copy migration",
    # virDomainShutdownReason
    ("in shutdown", "unknown"): "the reason is unknown",
    ("in shutdown", "user"): "shutting down on user request",
    # virDomainShutoffReason
    ("shut off", "unknown"): "the reason is unknown",
    ("shut off", "shutdown"): "normal shutdown",
    ("shut off", "destroyed"): "forced poweroff",
    ("shut off", "crashed"): "machine crashed",
    ("shut off", "migrated"): "migrated to another host",
    ("shut off", "saved"): "saved to a file",
    ("shut off", "failed"): "machine failed to start",
    (
        "shut off",
        "from snapshot",
    ): "restored from a snapshot which was taken while machine was shutoff",
    (
        "shut off",
        "daemon",
    ): "daemon decided to kill machine during reconnection processing",
}


def humanize_state(state: str, reason: str) -> tuple[str, str]:
    """Convert a ``virsh`` state/reason pair into human-friendly strings.

    Unknown states/reasons fall back to the raw input — no ``KeyError`` is
    raised. The contract matches the old ``_humanize_machine_status`` helper
    so caller-visible output is preserved across the libvirt-python -> virsh
    port.
    """
    status = DOMSTATE_HUMAN.get(state, state)
    human_reason = _REASON_HUMAN.get((state, reason), reason)
    return status, human_reason


# ---------------------------------------------------------------------------
# Convenience parsers around individual ``virsh`` subcommands.
# ---------------------------------------------------------------------------


def virsh_list_all_names(uri: str) -> list[str]:
    """Return all domain names known to libvirt at ``uri`` (running or not).

    Uses ``virsh list --all --name`` which prints one bare domain name per
    line. Blank lines (including the trailing newline) are stripped.
    """
    result = run_virsh(uri, ["list", "--all", "--name"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def virsh_domstate(uri: str, name: str) -> str:
    """Return the raw lowercase state string for ``name`` (e.g. ``running``)."""
    result = run_virsh(uri, ["domstate", name])
    return result.stdout.strip().lower()


def virsh_domstate_reason(uri: str, name: str) -> tuple[str, str]:
    """Return ``(state, reason)`` for ``name``.

    ``virsh domstate --reason`` emits a single line of the form
    ``<state> (<reason words>)``. Returns ``("", "")``-style empty strings if
    the output cannot be parsed, but in practice virsh always emits both.
    """
    result = run_virsh(uri, ["domstate", "--reason", name])
    text = result.stdout.strip().lower()
    if not text:
        return "", ""
    if "(" in text and text.endswith(")"):
        state_part, _, reason_part = text.partition("(")
        return state_part.strip(), reason_part[:-1].strip()
    return text, ""


def virsh_snapshot_names(uri: str, name: str) -> list[str]:
    """Return snapshot names for domain ``name`` in creation order.

    Uses ``virsh snapshot-list <name> --name`` which prints one snapshot name
    per line in creation order.
    """
    result = run_virsh(uri, ["snapshot-list", name, "--name"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def virsh_capabilities(uri: str) -> str:
    """Return the raw XML output of ``virsh capabilities``."""
    result = run_virsh(uri, ["capabilities"])
    return result.stdout


# ---------------------------------------------------------------------------
# Tempfile helper for XML handoff to ``virsh snapshot-create --xmlfile``.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _xml_tempfile(xml: str) -> Iterator[str]:
    """Yield a path to a tempfile containing ``xml``; clean up on exit.

    ``virsh snapshot-create --xmlfile -`` (stdin) is unreliable on RHEL 7-era
    ``virsh``. Using an on-disk tempfile is consistent across distros. The
    file is deleted even when the body raises; cleanup errors are swallowed
    silently because best-effort unlink is good enough for a temp file.
    """
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".xml",
            prefix="lvlab-snapshot-",
            delete=False,
        ) as fh:
            fh.write(xml)
            path = fh.name
        yield path
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
