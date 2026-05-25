"""tkc-lvlab — declarative libvirt+QEMU lab VM manager.

Exposes the installed package version as :data:`__version__` so the
console scripts (``lvlab``, ``createvm``, ``deletevm``) can surface it
via a ``--version`` flag.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tkc-lvlab")
except (
    PackageNotFoundError
):  # pragma: no cover - source checkout without install metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
