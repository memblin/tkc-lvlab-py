"""Unit tests for :mod:`tkc_lvlab.utils.subprocess_env`.

Locks in the contract that :func:`system_first_env` prepends the
system bin paths to ``PATH`` while preserving every other env var
exactly. The helper is what keeps ``virt-install`` on Debian 13
(``#!/usr/bin/env python3`` shebang) from resolving the interpreter
to the venv's Python — which can't ``import gi`` from the system
``python3-gi`` package.
"""

from __future__ import annotations

from unittest import mock

from tkc_lvlab.utils.subprocess_env import system_first_env


def test_system_first_env_prepends_system_paths_to_existing_path() -> None:
    """``/usr/bin:/usr/sbin`` lands first; the prior PATH is preserved."""
    with mock.patch.dict(
        "os.environ",
        {"PATH": "/home/user/.venv/bin:/usr/local/bin", "OTHER": "value"},
        clear=True,
    ):
        env = system_first_env()

    assert env["PATH"] == "/usr/bin:/usr/sbin:/home/user/.venv/bin:/usr/local/bin"
    assert env["OTHER"] == "value"


def test_system_first_env_handles_missing_path() -> None:
    """If PATH is unset, the helper still produces a sensible PATH."""
    with mock.patch.dict("os.environ", {"FOO": "bar"}, clear=True):
        env = system_first_env()

    assert env["PATH"] == "/usr/bin:/usr/sbin"
    assert env["FOO"] == "bar"


def test_system_first_env_handles_empty_path() -> None:
    """If PATH is explicitly empty, the helper falls back to system-only PATH."""
    with mock.patch.dict("os.environ", {"PATH": ""}, clear=True):
        env = system_first_env()

    assert env["PATH"] == "/usr/bin:/usr/sbin"


def test_system_first_env_returns_a_copy_not_a_reference() -> None:
    """Mutating the returned env must not leak back into ``os.environ``."""
    with mock.patch.dict("os.environ", {"PATH": "/orig"}, clear=True):
        env = system_first_env()
        env["NEW_VAR"] = "added"

        import os

        assert "NEW_VAR" not in os.environ
