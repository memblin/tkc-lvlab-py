"""Pytest configuration and shared safety scaffolding.

This module is loaded once per pytest session. It exists to enforce one
non-negotiable rule from ``CLAUDE.md``:

    No test, fixture, or teardown step ever touches a libvirt domain,
    qcow2 file, or ISO whose name does not start with the per-run test
    prefix.

The prefix is generated fresh on every session
(``lvlab-test-<epoch_ms>-<random4>-``) and exposed via the
:func:`make_test_name` helper and matching pytest fixture. The
:func:`assert_owned_by_test` guard MUST be called from every test helper
that performs a destructive operation (``virsh destroy``, ``undefine``,
``os.remove``, etc.).

Integration tests are gated behind ``LVLAB_INTEGRATION=1``. CI never sets
this — see ``TODO.md`` "Cross-cutting safety rules". The session-scoped
reaper at the bottom of the file only runs when integration is enabled,
and even then it only touches prefixed names — it never iterates over
the full libvirt domain list.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_lvlab_logger_propagation() -> Iterator[None]:
    """Reset the ``tkc_lvlab`` logger's ``propagate`` flag after every test.

    ``tkc_lvlab._logging.configure_logging`` sets ``propagate=False`` on
    the project root logger so production output doesn't double-log.
    Tests that drive the CLI through Typer's ``CliRunner`` invoke the
    root callback, which calls ``configure_logging`` and leaves
    propagation off — that breaks pytest's ``caplog`` capture for any
    test that runs after a CLI-driving test. This autouse fixture
    restores ``propagate=True`` before each test so ``caplog`` keeps
    working regardless of test ordering.
    """
    yield
    logging.getLogger("tkc_lvlab").propagate = True


LVLAB_TEST_PREFIX: str = f"lvlab-test-{int(time.time() * 1000)}-{secrets.token_hex(2)}-"
"""Session-unique prefix every test-owned libvirt/qemu resource must start with.

Generated once at import time. The epoch-ms component avoids collisions
between sequential runs; the random suffix avoids collisions between
parallel runs on the same machine.
"""


_INTEGRATION_ENV_VAR = "LVLAB_INTEGRATION"


def make_test_name(base: str) -> str:
    """Return a test-owned resource name carrying :data:`LVLAB_TEST_PREFIX`.

    This is the only sanctioned way for a test to name a libvirt domain,
    qcow2 file, or cloud-init ISO. Names produced here are the only names
    the session-scoped reaper is permitted to touch.

    Args:
        base: A short, human-meaningful suffix (e.g. ``"alpha"``,
            ``"snapshot-rollback"``). Must not be empty.

    Returns:
        A string of the form ``f"{LVLAB_TEST_PREFIX}{base}"``.

    Raises:
        ValueError: If ``base`` is empty or whitespace-only.
    """
    if not base or not base.strip():
        raise ValueError("make_test_name(base): base must be non-empty")
    return f"{LVLAB_TEST_PREFIX}{base}"


def _is_owned_by_test(name: str) -> bool:
    """Return True iff ``name`` carries the per-session test prefix.

    Two patterns are recognized as test-owned:

    1. ``LVLAB_TEST_PREFIX`` at the start of ``name`` — the normal case
        for resources whose name is built directly via
        :func:`make_test_name`.
    2. ``f"oneoff-{LVLAB_TEST_PREFIX}"`` at the start of ``name`` — the
        :mod:`tkc_lvlab.scripts.createvm` case, where the standalone
        script hard-codes a leading ``oneoff-`` on every domain it
        creates (Phase 6 collision-prevention). Tests pass a
        ``make_test_name(...)`` value as the user-facing ``vm_name`` and
        the resulting libvirt domain name becomes
        ``oneoff-{LVLAB_TEST_PREFIX}{base}``, which the reaper still
        needs to recognize.

    Args:
        name: The libvirt domain name or filesystem path basename.

    Returns:
        True if either pattern matches.
    """
    return name.startswith(LVLAB_TEST_PREFIX) or name.startswith(
        f"oneoff-{LVLAB_TEST_PREFIX}"
    )


def assert_owned_by_test(name: str) -> None:
    """Raise unless ``name`` is a test-owned resource.

    Call this before any destructive operation in a test helper —
    ``virsh destroy``, ``virsh undefine``, ``os.remove``, ``shutil.rmtree``,
    snapshot deletion, etc. If a name without the prefix slips through,
    we fail loudly rather than risk damaging a developer VM.

    Recognizes both the plain ``LVLAB_TEST_PREFIX`` form and the
    ``oneoff-{LVLAB_TEST_PREFIX}`` form that ``createvm`` produces —
    see :func:`_is_owned_by_test`.

    Args:
        name: The libvirt domain name or filesystem path basename.

    Raises:
        AssertionError: If ``name`` does not match either recognized
            test-owned pattern.
    """
    if not _is_owned_by_test(name):
        raise AssertionError(
            f"Refusing to operate on {name!r}: not owned by this test session "
            f"(expected prefix {LVLAB_TEST_PREFIX!r} or "
            f"{f'oneoff-{LVLAB_TEST_PREFIX}'!r}). This guard exists to "
            f"prevent test teardown from touching developer VMs."
        )


@pytest.fixture(scope="session")
def lvlab_test_prefix() -> str:
    """Expose :data:`LVLAB_TEST_PREFIX` as a session-scoped fixture.

    Returns:
        The per-session resource-name prefix.
    """
    return LVLAB_TEST_PREFIX


@pytest.fixture
def test_name() -> "callable[[str], str]":
    """Return the :func:`make_test_name` helper as a fixture.

    Lets tests write ``test_name("alpha")`` instead of importing
    :func:`make_test_name` directly. Both forms are supported.

    Returns:
        The :func:`make_test_name` function.
    """
    return make_test_name


@pytest.fixture(scope="session")
def lvlab_test_basedir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Provide a session-scoped temp directory for test-owned on-disk artifacts.

    Use this instead of the developer's shared
    ``~/.local/lvlab/...`` directory whenever an integration test needs
    a place to write qcow2 disks or cloud-init ISOs.

    Args:
        tmp_path_factory: pytest's built-in temp directory factory.

    Returns:
        Path to a fresh per-session directory.
    """
    return tmp_path_factory.mktemp("lvlab-test-basedir")


def _integration_enabled() -> bool:
    """Return True iff ``LVLAB_INTEGRATION=1`` is set in the environment."""
    return os.environ.get(_INTEGRATION_ENV_VAR) == "1"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``integration`` marker.

    Marker registration here AND in ``pyproject.toml`` is redundant but
    intentional: the pyproject entry is what suppresses pytest's
    "unknown marker" warning, this entry is what shows up in
    ``pytest --markers`` with a meaningful help string.

    Args:
        config: pytest configuration object.
    """
    config.addinivalue_line(
        "markers",
        "integration: test exercises real virsh/qemu-img/libvirt; "
        f"gated by {_INTEGRATION_ENV_VAR}=1 (default: skipped).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every ``@pytest.mark.integration`` test unless explicitly enabled.

    The default for every developer machine and every CI runner is
    "no integration". Opt in by exporting ``LVLAB_INTEGRATION=1`` before
    invoking pytest. CI **must never** set this on a shared runner.

    Args:
        config: pytest configuration object (unused, but part of the hook
            signature).
        items: the collected test items, modified in place.
    """
    del config  # unused
    if _integration_enabled():
        return
    skip_marker = pytest.mark.skip(
        reason=f"integration test (set {_INTEGRATION_ENV_VAR}=1 to enable)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


def _reap_test_prefixed_domains(uri: str) -> None:
    """Destroy + undefine any leftover domain whose name starts with the prefix.

    Called at session teardown when integration tests ran. Walks ONLY
    domains matching :data:`LVLAB_TEST_PREFIX`; never lists all domains
    unconditionally. Each name is checked against
    :func:`assert_owned_by_test` before any destructive op.

    Errors during reap are written to stderr but do not propagate —
    teardown is best-effort. A leaked test domain is a bug to fix in the
    failing test, but it must not mask the test result.

    Args:
        uri: libvirt URI to scan (e.g. ``qemu:///session``).
    """
    try:
        result = subprocess.run(
            ["virsh", "-c", uri, "list", "--all", "--name"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        # virsh binary not installed — nothing to reap.
        return
    if result.returncode != 0:
        return
    for raw in result.stdout.splitlines():
        name = raw.strip()
        if not name or not _is_owned_by_test(name):
            continue
        assert_owned_by_test(name)  # belt-and-suspenders
        # Best-effort destroy (may already be off) then undefine.
        subprocess.run(
            ["virsh", "-c", uri, "destroy", name],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["virsh", "-c", uri, "undefine", name, "--remove-all-storage"],
            capture_output=True,
            check=False,
        )


@pytest.fixture(scope="session", autouse=True)
def _session_reaper() -> Iterator[None]:
    """Session-scoped teardown that reaps prefixed leftovers after integration runs.

    No-op unless integration is enabled. Even when enabled, only touches
    domains whose name starts with :data:`LVLAB_TEST_PREFIX` (or the
    ``oneoff-`` prefixed variant — see :func:`_is_owned_by_test`). This
    is the last line of defense against a crashing integration test
    leaving state behind — it must NEVER expand its scope to "all
    domains".
    """
    yield
    if not _integration_enabled():
        return
    # Integration runs use whichever URI the test specifies; the reaper
    # checks the conventional ones. Adding URIs here is fine; removing
    # the prefix guard is not.
    for uri in ("qemu:///session", "qemu:///system"):
        _reap_test_prefixed_domains(uri)
    _reap_test_prefixed_storage()


# ---------------------------------------------------------------------------
# Integration test URI selection
# ---------------------------------------------------------------------------


_INTEGRATION_URIS: tuple[str, ...] = ("qemu:///session", "qemu:///system")

# Dedicated test-only storage root, kept distinct from the production
# ``/var/lib/libvirt/images/oneoff/`` directory that ``createvm`` uses
# by default. Tests must pass ``--storage-root <this path>`` to both
# ``createvm`` and ``destroyvm`` so per-VM artifacts never share a
# parent dir with a real user's one-off VMs.
#
# Convention: createvm.py already passes ``exist_ok=False`` to the
# per-VM ``mkdir`` (see ``_session_reaper`` cleanup + the
# ``FileExistsError`` raise in the script), so a stale prefixed
# directory from a crashed prior test will cause the next createvm
# call for the same name to fail with a clear error rather than
# silently overwriting state. The session storage reaper below sweeps
# leftover prefixed directories at session end as the safety net.
_LVLAB_TEST_STORAGE_ROOT: Path = Path("/var/lib/libvirt/images/lvlab-test")


def _virsh_probe(uri: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run a short, locale-stable ``virsh`` probe with a tight timeout.

    Args:
        uri: libvirt URI to probe.
        *args: Remaining ``virsh`` arguments.

    Returns:
        The :class:`subprocess.CompletedProcess`, or ``None`` if
        ``virsh`` is missing or the probe timed out — both are treated
        as "URI not usable" by the higher-level checks.
    """
    try:
        return subprocess.run(
            ["virsh", "-c", uri, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _uri_is_test_ready(uri: str) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for an integration URI.

    Three checks, in order — first failure short-circuits with a
    human-readable reason the skip message can surface:

    1. ``virsh -c <uri> list`` exits 0 — i.e. the test user can talk
        to the daemon at all. For ``qemu:///system`` this is the
        libvirt-group membership check; sudo elevation is never
        attempted.
    2. The ``default`` libvirt network exists — **only required for
        URIs that use a managed network**. ``qemu:///session`` runs
        with user-mode networking (Phase 12) and so has no need for
        a libvirt network; the check is skipped for session URIs.
    3. The dedicated test storage root
        (``/var/lib/libvirt/images/lvlab-test/``) can be created or is
        writable by the test user. Without this, ``virt-install``
        cannot place per-VM disks. Tests use this path instead of
        createvm's production default to keep test artifacts from
        ever sharing a parent dir with a real user's one-off VMs.

    Args:
        uri: libvirt URI to probe.

    Returns:
        ``(True, "")`` if the URI is ready for integration tests;
        ``(False, reason)`` otherwise.
    """
    list_probe = _virsh_probe(uri, "list")
    if list_probe is None or list_probe.returncode != 0:
        return False, (
            f"cannot reach {uri} (no libvirt-group access or daemon "
            f"unavailable) — sudo elevation not attempted"
        )

    # qemu:///session uses user-mode networking under Phase 12, so the
    # 'default' libvirt network is irrelevant on that URI. For
    # qemu:///system (and any other managed-network URI) the test
    # manifest still attaches to 'default' and the check stays in
    # force.
    if "session" not in uri:
        net_probe = _virsh_probe(uri, "net-list", "--name")
        if net_probe is None or net_probe.returncode != 0:
            return False, f"cannot list networks on {uri}"
        if "default" not in {line.strip() for line in net_probe.stdout.splitlines()}:
            return False, (
                f"no 'default' libvirt network on {uri} — tests use the "
                f"createvm default --network setting which requires it"
            )

    # mkdir is idempotent; the existence check after is what matters.
    # mode=0o755 so qemu (under qemu:///system) can traverse + read.
    try:
        _LVLAB_TEST_STORAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    except PermissionError:
        return False, (
            f"test storage root {_LVLAB_TEST_STORAGE_ROOT} cannot be "
            f"created (parent /var/lib/libvirt/images/ not writable by "
            f"test user); add user to the libvirt group or adjust "
            f"ownership"
        )
    if not os.access(_LVLAB_TEST_STORAGE_ROOT, os.W_OK):
        return False, (
            f"test storage root {_LVLAB_TEST_STORAGE_ROOT} exists but "
            f"is not writable by the test user"
        )

    return True, ""


@pytest.fixture(scope="session", params=_INTEGRATION_URIS)
def integration_uri(request: pytest.FixtureRequest) -> str:
    """Parametrize each integration test across reachable libvirt URIs.

    Yields ``qemu:///session`` and ``qemu:///system`` when each is
    test-ready (see :func:`_uri_is_test_ready` for the three-part
    readiness probe). URIs that aren't ready are skipped per-parameter,
    not per-test, so a partially-equipped host still gets coverage on
    the URIs that work.

    Returns:
        A test-ready libvirt URI string. The test is skipped (via
        :func:`pytest.skip`) for URIs that fail any readiness check.
    """
    uri = request.param
    ready, reason = _uri_is_test_ready(uri)
    if not ready:
        pytest.skip(reason)
    return uri


@pytest.fixture(scope="session")
def lvlab_integration_storage_root() -> Path:
    """Expose the libvirt-readable test storage root.

    Tests pass this path to ``createvm`` / ``destroyvm`` via
    ``--storage-root`` so per-VM artifacts land in a dedicated,
    qemu-traversable directory — distinct from the production
    ``/var/lib/libvirt/images/oneoff/`` that real users' one-off VMs
    occupy.

    The directory is created by :func:`_uri_is_test_ready` during
    URI-readiness probing (so the readiness skip message can call out
    a permission problem before any test body runs). createvm will
    refuse to overwrite an existing per-VM subdir
    (``mkdir(exist_ok=False)`` in the script), so a leaked prefixed
    directory from a crashed prior run becomes a loud failure rather
    than a silent overwrite.

    Returns:
        Path to ``/var/lib/libvirt/images/lvlab-test/``.
    """
    return _LVLAB_TEST_STORAGE_ROOT


def _reap_test_prefixed_storage() -> None:
    """Remove any per-VM storage dir under the test storage root.

    Companion to :func:`_reap_test_prefixed_domains`. The domain reaper
    handles libvirt state; this handles the qcow2 / cloud-init ISO
    state that ``destroyvm`` would normally remove but which can
    survive a crashing test. Walks ONLY directories matching the
    per-session prefix (plain or ``oneoff-`` prefixed); never iterates
    over unrelated entries.

    Operates on :data:`_LVLAB_TEST_STORAGE_ROOT` only — never the
    production ``/var/lib/libvirt/images/oneoff/`` directory, even
    though both share a parent.
    """
    if not _LVLAB_TEST_STORAGE_ROOT.exists():
        return
    for child in _LVLAB_TEST_STORAGE_ROOT.iterdir():
        if not child.is_dir():
            continue
        if not _is_owned_by_test(child.name):
            continue
        assert_owned_by_test(child.name)
        try:
            shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass
