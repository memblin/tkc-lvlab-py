"""Shared CLI output helpers: one table style + a TTY-vs-pipe gate.

Establishes the single human-facing output style the ``lvlab`` commands
converge on (issue #103, the foundation of the #107 output-unification
epic). The ``lvlab global show instances`` Rich table is the reference
aesthetic; this module factors that out so ``status`` / ``init`` /
``smoke`` render consistently, and provides the TTY detection that lets
live views degrade to plain lines when stdout is piped, redirected, or
running under CI.

Audience split (see #107):

- **Human-facing** summaries/tables route through here. Static tables
    render through :func:`get_console`, which widens a non-interactive
    console so cells aren't clipped — a Rich table printed to a pipe is
    already plain (no ANSI), so it stays readable in logs.
- **Live** views (progress, phase tables) should consult :func:`is_tty`
    and fall back to plain incremental lines off a terminal, since a Rich
    ``Live`` emits cursor-control escapes that garble captured logs.
- **Machine-facing** output (``ssh-config``, ``hosts``, ``cloudinit``,
    any ``--format json|yaml``) must NOT route through here — it stays
    raw so piping keeps working.
"""

from __future__ import annotations

import os
import sys

from rich.box import Box, SQUARE
from rich.console import Console
from rich.table import Table


# Width used for a non-interactive console (piped output, the test
# runner). Rich defaults those to 80 columns and *truncates* cells that
# overflow, which would clip long domain names, URIs, and image URLs.
# Wide enough that the project's tables render in full; the ``COLUMNS``
# env var still wins when the user sets it.
NON_TTY_WIDTH = 200


def get_console(*, stderr: bool = False) -> Console:
    """Return the shared console, widened when stdout is not a TTY.

    Use for static, human-facing tables. When attached to a terminal the
    terminal's width is honored; otherwise the console is widened to
    :data:`NON_TTY_WIDTH` so a piped/redirected table isn't clipped. A
    user-set ``COLUMNS`` always wins.

    Args:
        stderr: Render to stderr instead of stdout.

    Returns:
        A configured :class:`rich.console.Console`.
    """
    console = Console(stderr=stderr)
    if not console.is_terminal and "COLUMNS" not in os.environ:
        return Console(stderr=stderr, width=NON_TTY_WIDTH)
    return console


def is_tty() -> bool:
    """Return ``True`` when stdout is an interactive terminal.

    Live/progress commands use this to decide between a Rich ``Live``
    view (terminal) and a plain-line fallback (pipe/redirect/CI), the
    #107 degradation rule. Reads ``sys.stdout`` directly so it reflects
    the real stream rather than a transient console instance.

    Returns:
        ``True`` if ``sys.stdout`` is attached to a terminal.
    """
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def styled_table(title: str | None = None, *, box: Box = SQUARE) -> Table:
    """Return a :class:`~rich.table.Table` in the shared lvlab style.

    Centralizes the title/header conventions so every tabular command
    reads the same. Callers add columns/rows on the returned table.

    Args:
        title: Optional table title rendered above the grid.
        box: Box-drawing style; defaults to a clean square border.

    Returns:
        An empty styled :class:`rich.table.Table`.
    """
    return Table(
        title=title,
        box=box,
        title_style="bold",
        header_style="bold cyan",
        title_justify="left",
    )
