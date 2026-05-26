"""Centralized exception hierarchy for lvlab.

Every error lvlab raises on purpose descends from :class:`LvlabError`, so a
single ``except LvlabError`` at the CLI boundary can catch any
library-level failure and convert it into a clean ``typer.Exit`` instead of
leaking a traceback. The library layers (``utils/*``, :mod:`tkc_lvlab.config`)
raise these exceptions; only :mod:`tkc_lvlab.cli` is allowed to translate
them into Typer exit codes — the library never imports ``typer``.

This module is **dependency-free by design**: it imports nothing from the
rest of the package. Several of the leaf exceptions are re-exported from the
``utils`` modules that historically defined them (e.g.
``from .exceptions import VirshError`` in :mod:`tkc_lvlab.utils.virsh`), so
the definitions must live somewhere those modules can import without risking
a circular import. Keep it that way: do not add intra-package imports here.

Hierarchy::

    LvlabError
    ├── ConfigError        — manifest cannot be read / structurally invalid
    │   └── ManifestError  — semantic manifest-validation failure
    ├── VirshError         — a ``virsh`` invocation failed
    ├── LibvirtNetworkError — libvirt network info unresolvable / invalid
    ├── DependencyError    — a required host binary is missing
    ├── OsInfoLookupError  — virt-install osinfo enumeration failed
    ├── PasswordHashError  — password hash could not be generated
    └── PublicKeyError     — SSH public key failed validation (also ValueError)

The split between :class:`ConfigError` and :class:`ManifestError` exists so a
later, larger refactor can convert today's sentinel return values
(``None``/``False``/``-1``) into raised exceptions cheaply: structural read
failures stay :class:`ConfigError`, while richer per-field validation gets
its own :class:`ManifestError` subclass without disturbing existing
``except ConfigError`` sites (which still catch it via inheritance).
"""

from __future__ import annotations


class LvlabError(Exception):
    """Base class for every error lvlab raises deliberately.

    Catching :class:`LvlabError` at the CLI boundary catches any
    library-level failure lvlab is responsible for, leaving genuine
    programming errors (``TypeError``, ``KeyError`` from a real bug) to
    surface as tracebacks. Library modules raise subclasses of this; the
    translation to ``typer.Exit`` happens only in :mod:`tkc_lvlab.cli`.
    """


class ConfigError(LvlabError):
    """Raised when a manifest cannot be read or is structurally invalid.

    Covers the file-level / structural failures of
    :func:`tkc_lvlab.config.parse_config`: the manifest parsed to something
    that is not a mapping, or is missing a required top-level section
    (``environment`` / ``images``). A *missing* ``Lvlab.yml`` is deliberately
    **not** a ``ConfigError`` — that path returns ``None`` (the long-standing
    soft-fail behavior) and is handled separately by callers.
    """


class ManifestError(ConfigError):
    """Raised on a semantic manifest-validation failure.

    A subclass of :class:`ConfigError` so existing ``except ConfigError``
    sites keep catching it. Reserved for richer per-field validation a later
    phase will add (e.g. an unknown ``network_type``, a static IP on a
    user-mode interface) when those sentinel checks are converted to raises.
    Not yet raised anywhere — defined now so the conversion stays cheap.
    """


class VirshError(LvlabError, RuntimeError):
    """Raised when a ``virsh`` invocation fails.

    Attributes:
        returncode: Exit code from ``virsh`` (or ``-1`` for timeouts, ``127``
            when the binary is missing).
        stderr: Captured stderr text (stripped). Empty if not available.
        args: The argument list passed to ``virsh`` (without the leading
            ``virsh -c <uri>``).
    """

    def __init__(self, returncode: int, stderr: str, args: list[str]) -> None:
        """Initialize the error from a failed ``virsh`` invocation.

        Args:
            returncode: Exit code from ``virsh`` (``-1`` for timeouts,
                ``127`` when the binary is missing).
            stderr: Captured stderr text; stored stripped (empty if
                unavailable).
            args: The ``virsh`` argv (without the leading ``virsh -c <uri>``)
                that failed.
        """
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
        """Render the formatted ``virsh ... failed`` message lazily."""
        return (
            f"virsh {' '.join(self.args)} failed (rc={self.returncode}): "
            f"{self.stderr or '<no stderr>'}"
        )


class LibvirtNetworkError(LvlabError, RuntimeError):
    """Raised when libvirt network information cannot be resolved or validated.

    Wraps two error surfaces:

    - **Discovery failure** — ``virsh net-dumpxml`` failed, the output was
        not parseable XML, or the named network does not exist.
    - **Policy failure** — a bridge network was used without explicit
        gateway/DNS, or the forward mode is one we don't support.

    Both are operator-actionable errors; the message names the specific
    failure.
    """


class DependencyError(LvlabError, RuntimeError):
    """Raised when one or more required host binaries are unavailable.

    The message string carries the per-binary breakdown and the
    package-manager-specific install command (when the OS family is
    recognized).
    """


class OsInfoLookupError(LvlabError, RuntimeError):
    """Raised when virt-install can't be consulted for available os-variants."""


class PasswordHashError(LvlabError, RuntimeError):
    """Raised when a password hash cannot be generated.

    The wrapped reason is either a missing ``openssl`` binary
    (``FileNotFoundError``) or a non-zero exit from ``openssl passwd``.
    Either way the message names the actual failure so the operator
    doesn't have to guess.
    """


class PublicKeyError(LvlabError, ValueError):
    """Raised when an SSH public key cannot be validated.

    Also subclasses :class:`ValueError` because call sites (and tests) in
    :mod:`tkc_lvlab.utils.ssh_keys` rely on catching ``ValueError`` — the
    ``except ValueError`` re-raise inside :func:`validate_public_key` and the
    discovery walk's ``except PublicKeyError`` both depend on that base.
    Removing it would silently widen what those handlers let through.
    """
