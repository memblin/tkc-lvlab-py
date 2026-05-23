"""Tests for the safety scaffolding in ``tests/conftest.py``.

These exist because the scaffolding itself is a load-bearing safety
guarantee — if ``make_test_name`` ever stopped prefixing, or
``assert_owned_by_test`` ever silently passed an unprefixed name, an
integration test could destroy a developer VM. The scaffolding is no
more allowed to have unnoticed bugs than the production code is.

What's locked in here:

- ``LVLAB_TEST_PREFIX`` always begins with ``lvlab-test-``.
- ``make_test_name`` always returns a string carrying that prefix.
- ``make_test_name`` rejects empty / whitespace-only bases.
- ``assert_owned_by_test`` raises on names that don't carry the prefix.
- The ``integration`` marker is registered AND, by default, integration
    tests are skipped (not just collected — actually skipped) because
    ``LVLAB_INTEGRATION`` is unset in this environment.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import (
    LVLAB_TEST_PREFIX,
    assert_owned_by_test,
    make_test_name,
)


def test_lvlab_test_prefix_shape() -> None:
    """The prefix follows the documented ``lvlab-test-<epoch_ms>-<random4>-`` shape."""
    assert LVLAB_TEST_PREFIX.startswith("lvlab-test-"), LVLAB_TEST_PREFIX
    assert LVLAB_TEST_PREFIX.endswith("-"), LVLAB_TEST_PREFIX
    # Three dash-separated body segments after the leading 'lvlab-test-':
    # epoch_ms, random_hex, then the trailing empty segment from the final '-'.
    body = LVLAB_TEST_PREFIX[len("lvlab-test-") :]
    parts = body.split("-")
    assert len(parts) >= 3, parts
    epoch_part, random_part = parts[0], parts[1]
    assert epoch_part.isdigit(), epoch_part
    assert len(random_part) == 4, random_part
    assert all(c in "0123456789abcdef" for c in random_part), random_part


def test_make_test_name_carries_prefix() -> None:
    """Every name produced by make_test_name starts with the session prefix."""
    name = make_test_name("alpha")
    assert name.startswith(LVLAB_TEST_PREFIX)
    assert name.endswith("alpha")


def test_make_test_name_rejects_empty_base() -> None:
    """An empty or whitespace-only base must be rejected — that's the trap we're guarding."""
    with pytest.raises(ValueError):
        make_test_name("")
    with pytest.raises(ValueError):
        make_test_name("   ")


def test_assert_owned_by_test_accepts_prefixed_name() -> None:
    """A name from make_test_name passes the guard with no exception."""
    name = make_test_name("beta")
    # Must not raise.
    assert_owned_by_test(name)


def test_assert_owned_by_test_rejects_unprefixed_name() -> None:
    """A name without the prefix must trip the guard — that's the whole point."""
    # A perfectly plausible developer VM name. If this ever passes, the
    # safety net is broken.
    with pytest.raises(AssertionError, match="not owned by this test session"):
        assert_owned_by_test("my-developer-vm")


def test_assert_owned_by_test_rejects_almost_prefix() -> None:
    """A name with a near-miss prefix (off-by-one) must still be rejected."""
    # Strip the trailing dash; this is a startswith() guard so trimming
    # the prefix's terminator makes it stop matching.
    near_miss = LVLAB_TEST_PREFIX.rstrip("-")  # missing trailing '-'
    with pytest.raises(AssertionError):
        assert_owned_by_test(near_miss)


@pytest.mark.integration
def test_integration_marker_is_actually_skipped_by_default() -> None:
    """A test marked integration must NOT execute when LVLAB_INTEGRATION is unset.

    If this body ever runs in a default pytest invocation, the
    ``pytest_collection_modifyitems`` skip hook has regressed.
    """
    assert (
        os.environ.get("LVLAB_INTEGRATION") == "1"
    ), "integration test body executed without LVLAB_INTEGRATION=1 — skip hook regressed"


def test_lvlab_test_prefix_fixture_matches_module_constant(
    lvlab_test_prefix: str,
) -> None:
    """The fixture exposes the same value as the module-level constant."""
    assert lvlab_test_prefix == LVLAB_TEST_PREFIX


def test_test_name_fixture_returns_make_test_name(test_name) -> None:
    """The ``test_name`` fixture returns the make_test_name function itself."""
    assert test_name is make_test_name
    assert test_name("gamma").startswith(LVLAB_TEST_PREFIX)
