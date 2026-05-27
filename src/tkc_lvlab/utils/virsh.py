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
import time
from dataclasses import dataclass
from typing import Iterator

# Re-export so existing imports (``from tkc_lvlab.utils.virsh import
# VirshError``) and isinstance checks keep working after the class definition
# moved to the centralized hierarchy in :mod:`tkc_lvlab.exceptions`.
from ..exceptions import VirshError

# Transient libvirtd connection-drop retry (issue #129). Under high concurrency
# the DBus-activated modular daemons (``virtqemud``) occasionally refuse a
# connection for a moment; a single ``virsh`` call then fails with one of the
# stderr signatures below even though the request never reached the daemon.
# Retrying is safe even for mutating verbs precisely because a *connection*
# failure means the command never ran. The schedule's length is also the retry
# count: one initial attempt plus ``len(_CONNECTION_ERROR_BACKOFF)`` retries.
_CONNECTION_ERROR_BACKOFF: tuple[float, ...] = (0.5, 1.0, 2.0)

# Lowercase stderr substrings that mark a transient connection drop. Matched
# case-insensitively against ``virsh`` stderr. These are connection-level only
# — genuine command errors (bad args, missing domain) are never listed, so they
# still surface on the first attempt.
_TRANSIENT_CONNECTION_MARKERS: tuple[str, ...] = (
    "remote peer disconnected",
    "failed to connect to the hypervisor",
    "cannot recv data",
    "broken pipe",
    "noreply",
)


def _is_transient_connection_error(stderr: str) -> bool:
    """Return whether ``virsh`` stderr signals a transient libvirtd connection drop.

    Matches the connection-level signatures from issue #129 case-insensitively.
    Genuine command failures (bad arguments, missing domain, inactive network)
    do not match, so only connection drops are treated as retry-worthy.

    Args:
        stderr: The captured ``virsh`` stderr text.

    Returns:
        ``True`` iff a connection-drop marker appears in ``stderr``.
    """
    haystack = stderr.lower()
    return any(marker in haystack for marker in _TRANSIENT_CONNECTION_MARKERS)


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

    A transient libvirtd connection drop (issue #129) is retried with backoff
    (``_CONNECTION_ERROR_BACKOFF``) rather than surfaced on the first failure:
    when ``capture=True`` and stderr matches a connection-level signature (see
    :func:`_is_transient_connection_error`), the call is repeated up to three
    times. This applies regardless of ``check`` and of the verb — a connection
    failure means the command never reached the daemon, so even a mutating verb
    is safe to retry. Genuine command failures (bad args, missing domain) are
    not retried; a missing binary and a timeout still raise immediately.

    Args:
        uri: libvirt connection URI (e.g. ``qemu:///session``).
        args: ``virsh`` subcommand and flags (no leading ``virsh -c <uri>``).
        check: If ``True``, raise :class:`VirshError` on nonzero exit.
        capture: If ``True``, capture stdout/stderr as text. If ``False``,
            inherit the parent's stdio (used for interactive subcommands).
            Connection-drop retry requires ``capture=True`` to read stderr.
        timeout: Wall-clock seconds before raising :class:`VirshError`. The
            default is 30s; long-running ops (snapshots) should pass a larger
            value explicitly.
        input_text: Optional stdin text. Reserved for future interactive use;
            no current Phase 2 caller passes this.

    Returns:
        The :class:`subprocess.CompletedProcess` from the invocation.

    Raises:
        VirshError: On nonzero exit (when ``check=True``), on missing
            ``virsh`` binary, or on timeout. A transient connection drop only
            raises after the retry budget is exhausted.
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

    for attempt in range(len(_CONNECTION_ERROR_BACKOFF) + 1):
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

        # Retry only a transient connection drop, and only while attempts
        # remain; everything else (success, or a genuine failure) falls
        # through to the check below on this attempt's result.
        if (
            capture
            and result.returncode != 0
            and attempt < len(_CONNECTION_ERROR_BACKOFF)
            and _is_transient_connection_error(result.stderr or "")
        ):
            time.sleep(_CONNECTION_ERROR_BACKOFF[attempt])
            continue
        break

    if check and result.returncode != 0:
        stderr = result.stderr if capture else ""
        raise VirshError(result.returncode, stderr or "", args)

    return result


# ---------------------------------------------------------------------------
# State strings — what ``virsh domstate`` actually emits.
# ---------------------------------------------------------------------------

# Per-state name constants. These mirror the lowercase strings ``virsh
# domstate`` emits (see libvirt's ``virDomainState`` enum). Extracting them
# avoids duplicating the literals across the maps and set memberships below.
DOMSTATE_NO_STATE = "no state"
DOMSTATE_RUNNING = "running"
DOMSTATE_IDLE = "idle"
DOMSTATE_PAUSED = "paused"
DOMSTATE_IN_SHUTDOWN = "in shutdown"
DOMSTATE_SHUT_OFF = "shut off"
DOMSTATE_CRASHED = "crashed"
DOMSTATE_PMSUSPENDED = "pmsuspended"

DOMSTATE_HUMAN: dict[str, str] = {
    # https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainState
    DOMSTATE_NO_STATE: "no state",
    DOMSTATE_RUNNING: "the machine is running",
    DOMSTATE_IDLE: "the machine is blocked on resource",
    DOMSTATE_PAUSED: "the machine is paused by user",
    DOMSTATE_IN_SHUTDOWN: "the machine is being shut down",
    DOMSTATE_SHUT_OFF: "the machine is shut off",
    DOMSTATE_CRASHED: "the machine is crashed",
    DOMSTATE_PMSUSPENDED: "the machine is suspended by guest power management",
}

RUNNING_STATES: set[str] = {DOMSTATE_RUNNING, DOMSTATE_PAUSED}
DEAD_STATES: set[str] = {DOMSTATE_SHUT_OFF, DOMSTATE_CRASHED}
SHUTDOWNABLE_STATES: set[str] = {
    DOMSTATE_RUNNING,
    DOMSTATE_IDLE,
    DOMSTATE_PAUSED,
    DOMSTATE_PMSUSPENDED,
}
DESTROYABLE_STATES: set[str] = {DOMSTATE_RUNNING, DOMSTATE_PAUSED}

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
    (DOMSTATE_RUNNING, "unknown"): "Unknown",
    (DOMSTATE_RUNNING, "booted"): "normal startup from boot",
    (DOMSTATE_RUNNING, "migrated"): "migrated from another host",
    (DOMSTATE_RUNNING, "restored"): "restored from a state file",
    (DOMSTATE_RUNNING, "from snapshot"): "restored from snapshot",
    (DOMSTATE_RUNNING, "unpaused"): "returned from paused state",
    (DOMSTATE_RUNNING, "migration canceled"): "returned from migration",
    (DOMSTATE_RUNNING, "save canceled"): "returned from failed save process",
    (DOMSTATE_RUNNING, "wakeup"): "returned from pmsuspended due to wakeup event",
    (DOMSTATE_RUNNING, "crashed"): "resumed from crashed",
    (DOMSTATE_RUNNING, "post-copy"): "running in post-copy migration mode",
    (DOMSTATE_RUNNING, "post-copy failed"): "running in failed post-copy migration",
    # virDomainShutdownReason
    (DOMSTATE_IN_SHUTDOWN, "unknown"): "the reason is unknown",
    (DOMSTATE_IN_SHUTDOWN, "user"): "shutting down on user request",
    # virDomainShutoffReason
    (DOMSTATE_SHUT_OFF, "unknown"): "the reason is unknown",
    (DOMSTATE_SHUT_OFF, "shutdown"): "normal shutdown",
    (DOMSTATE_SHUT_OFF, "destroyed"): "forced poweroff",
    (DOMSTATE_SHUT_OFF, "crashed"): "machine crashed",
    (DOMSTATE_SHUT_OFF, "migrated"): "migrated to another host",
    (DOMSTATE_SHUT_OFF, "saved"): "saved to a file",
    (DOMSTATE_SHUT_OFF, "failed"): "machine failed to start",
    (
        DOMSTATE_SHUT_OFF,
        "from snapshot",
    ): "restored from a snapshot which was taken while machine was shutoff",
    (
        DOMSTATE_SHUT_OFF,
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


@dataclass(frozen=True)
class DomInfo:
    """Cheap per-domain facts parsed from a single ``virsh dominfo`` call.

    Holds only the fields a ``virsh dominfo`` read returns without stunning a
    running guest — no live CPU/disk/net sampling. ``vcpus`` and
    ``max_memory_kib`` are ``None`` when the corresponding line is absent or
    unparseable (e.g. a transient domain mid-define), so callers must tolerate
    missing values rather than assume an int is always present.

    Attributes:
        state: Lowercase state string as ``virsh`` emits it (e.g. ``running``,
            ``shut off``).
        vcpus: Allocated vCPU count from the ``CPU(s):`` line, or ``None``.
        max_memory_kib: Max memory in KiB from the ``Max memory:`` line, or
            ``None``.
        autostart: ``True`` when the ``Autostart:`` line reads ``enable``.
        persistent: ``True`` when the ``Persistent:`` line reads ``yes``.
    """

    state: str
    vcpus: int | None
    max_memory_kib: int | None
    autostart: bool
    persistent: bool


def virsh_dominfo(uri: str, name: str) -> DomInfo:
    """Return parsed :class:`DomInfo` for domain ``name`` via one ``virsh`` call.

    Runs ``virsh dominfo <name>`` once (the only sanctioned cheap read for the
    cross-connection overview) and parses its colon-delimited ``Field: value``
    lines. The locale is forced to ``C`` by :func:`run_virsh`, so the field
    labels (``State``, ``CPU(s)``, ``Max memory``, ``Autostart``,
    ``Persistent``) are stable across hosts.

    The ``Max memory: <n> KiB`` value keeps only the leading integer (the unit
    suffix is dropped). Unrecognized or absent numeric lines leave the matching
    field ``None`` rather than raising.

    Args:
        uri: libvirt connection URI.
        name: The exact domain name to inspect.

    Returns:
        A :class:`DomInfo` with the cheap facts for ``name``.

    Raises:
        VirshError: On nonzero exit (e.g. domain missing) or a connection-level
            failure. The caller is expected to skip the whole connection when a
            ``VirshError`` surfaces.
    """
    result = run_virsh(uri, ["dominfo", name])

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()

    return DomInfo(
        state=fields.get("State", "").lower(),
        vcpus=_parse_leading_int(fields.get("CPU(s)")),
        max_memory_kib=_parse_leading_int(fields.get("Max memory")),
        autostart=fields.get("Autostart", "").lower() == "enable",
        persistent=fields.get("Persistent", "").lower() == "yes",
    )


def _parse_leading_int(value: str | None) -> int | None:
    """Return the leading integer token of ``value``, or ``None``.

    ``CPU(s):`` is a bare integer while ``Max memory:`` is ``<n> KiB``; both
    are handled by taking the first whitespace-delimited token. A missing line
    (``None``) or a non-integer first token yields ``None``.
    """
    if not value:
        return None
    token = value.split()[0]
    try:
        return int(token)
    except ValueError:
        return None


def vm_exists(uri: str, name: str) -> bool:
    """Return ``True`` when a libvirt domain named ``name`` is defined at ``uri``.

    Uses ``virsh dominfo`` with ``check=False`` so a missing domain (nonzero
    exit) reports ``False`` rather than raising. A missing ``virsh`` binary
    also reports ``False`` — the caller's dependency precheck is responsible
    for surfacing that earlier.

    Args:
        uri: libvirt connection URI.
        name: The exact domain name to look up.

    Returns:
        ``True`` iff ``virsh dominfo <name>`` exits zero.
    """
    try:
        result = run_virsh(uri, ["dominfo", name], check=False)
    except VirshError:
        return False
    return result.returncode == 0


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
