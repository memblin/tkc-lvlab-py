"""Helpers for sanitizing the environment of subprocess calls.

Used by code paths that shell out to system binaries whose shebangs
delegate via ``/usr/bin/env python3``. When lvlab runs inside a
uv-managed venv, ``.venv/bin`` is first on ``PATH``, so ``env python3``
resolves to the venv's interpreter — which does NOT have access to
system site-packages. Concretely, ``virt-install`` on Debian 13 uses
``#!/usr/bin/env python3`` and imports ``gi`` from the system
``python3-gi`` package; the venv interpreter can't import ``gi`` and
the call fails with ``ModuleNotFoundError: No module named 'gi'``.

This module exposes :func:`system_first_env` which returns a copy of
``os.environ`` with the standard system bin paths prepended to
``PATH``. Pass the result as the ``env=`` argument to
:func:`subprocess.run` when invoking host binaries that need the
host's interpreter rather than a venv-shadowed one.
"""

from __future__ import annotations

import os


def system_first_env() -> dict[str, str]:
    """Return a process env with ``/usr/bin`` and ``/usr/sbin`` first on ``PATH``.

    The returned dict is a copy of ``os.environ`` — all other variables
    are preserved exactly. Only ``PATH`` is modified.

    Returns:
        A dict suitable for the ``env=`` kwarg of
        :func:`subprocess.run` and friends.

    Example:
        >>> import subprocess
        >>> from tkc_lvlab.utils.subprocess_env import system_first_env
        >>> subprocess.run(["virt-install", ...], env=system_first_env())
    """
    env = dict(os.environ)
    current_path = env.get("PATH", "")
    if current_path:
        env["PATH"] = f"/usr/bin:/usr/sbin:{current_path}"
    else:
        env["PATH"] = "/usr/bin:/usr/sbin"
    return env
