"""Centralized logging configuration for the lvlab CLI.

The CLI previously emitted diagnostic output via a mix of ``print()`` and
``click.echo()`` calls. This module provides a single project-rooted logger
hierarchy (``tkc_lvlab``) so that diagnostic / informational / error output
can be routed consistently to stderr while leaving user-facing CLI output
(``click.echo``) on stdout.

Usage::

    from tkc_lvlab._logging import get_logger

    logger = get_logger(__name__)
    logger.info("Doing the thing")

The root group in :mod:`tkc_lvlab.cli` calls :func:`configure_logging` once
at startup, translating ``-v`` / ``-q`` into a level on the project root
logger. Module-level loggers obtained via :func:`get_logger` inherit that
level through the standard ``logging`` propagation chain.
"""

import logging
import sys

#: Root logger name for the whole project. All :func:`get_logger` calls
#: return descendants of this logger, so a single ``setLevel`` on it
#: controls verbosity for every module.
ROOT_LOGGER_NAME = "tkc_lvlab"

#: Format string used by the stderr handler. ``%(name)s`` makes it easy to
#: see which module produced a message once Phase 2 brings more call sites.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: Sentinel attribute set on the stderr handler so repeat invocations of
#: :func:`configure_logging` (e.g. from tests) replace it cleanly instead
#: of stacking duplicate handlers.
_HANDLER_ATTR = "_lvlab_stderr_handler"


def configure_logging(verbosity: int = 0, quiet: bool = False) -> logging.Logger:
    """Configure the project root logger.

    Args:
        verbosity: ``-v`` count from the CLI. ``0`` is the default (WARNING),
            ``1`` (``-v``) raises to INFO, ``2`` or more (``-vv``) raises to
            DEBUG.
        quiet: ``-q`` flag from the CLI. When true the logger is pinned to
            ERROR regardless of ``verbosity`` so destructive paths still
            surface failures even when the user has silenced chatter.

    Returns:
        The project root logger, already configured. Callers normally do not
        need this — :func:`get_logger` is the usual entry point.
    """
    if quiet:
        level = logging.ERROR
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(level)
    # Keep our hierarchy self-contained; don't double-log through the
    # global root if some downstream lib configures it.
    root.propagate = False

    # Replace any prior handler we installed so repeat calls (tests, REPL)
    # don't stack duplicates. Leave handlers added by other code alone.
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_ATTR, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, _HANDLER_ATTR, True)
    root.addHandler(handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the project root.

    ``name`` is normally ``__name__`` from the calling module. If the caller
    is already inside the ``tkc_lvlab`` package the returned logger is its
    standard dotted child; otherwise the name is grafted under the project
    root so log records still pick up our configured handler/level.
    """
    if name == ROOT_LOGGER_NAME or name.startswith(ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
