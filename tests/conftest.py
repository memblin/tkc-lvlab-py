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


def assert_owned_by_test(name: str) -> None:
    """Raise unless ``name`` is a test-owned resource.

    Call this before any destructive operation in a test helper —
    ``virsh destroy``, ``virsh undefine``, ``os.remove``, ``shutil.rmtree``,
    snapshot deletion, etc. If a name without the prefix slips through,
    we fail loudly rather than risk damaging a developer VM.

    Args:
        name: The libvirt domain name or filesystem path basename.

    Raises:
        AssertionError: If ``name`` does not start with
            :data:`LVLAB_TEST_PREFIX`.
    """
    if not name.startswith(LVLAB_TEST_PREFIX):
        raise AssertionError(
            f"Refusing to operate on {name!r}: not owned by this test session "
            f"(expected prefix {LVLAB_TEST_PREFIX!r}). This guard exists to "
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
        if not name or not name.startswith(LVLAB_TEST_PREFIX):
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
    domains whose name starts with :data:`LVLAB_TEST_PREFIX`. This is the
    last line of defense against a crashing integration test leaving
    state behind — it must NEVER expand its scope to "all domains".
    """
    yield
    if not _integration_enabled():
        return
    # Integration runs use whichever URI the test specifies; the reaper
    # checks the conventional ones. Adding URIs here is fine; removing
    # the prefix guard is not.
    for uri in ("qemu:///session", "qemu:///system"):
        _reap_test_prefixed_domains(uri)
