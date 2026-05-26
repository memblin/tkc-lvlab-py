"""Unit tests for :mod:`tkc_lvlab.utils.output` — the shared CLI output
helper (issue #103, foundation of the #107 output-unification epic).

What matters here:

- ``get_console`` widens a *non-interactive* console so piped/redirected
    tables aren't clipped (the whole reason the helper exists), while a
    user-set ``COLUMNS`` still wins.
- ``is_tty`` is the gate live views use to fall back to plain lines off a
    terminal.
- ``styled_table`` round-trips its title/headers/cells through a console.
"""

from __future__ import annotations

import io
import sys

from rich.console import Console

from tkc_lvlab.utils import output
from tkc_lvlab.utils.output import NON_TTY_WIDTH, get_console, is_tty, styled_table


def test_styled_table_round_trips_title_headers_and_cells() -> None:
    """A styled table renders its title, column headers, and row cells."""
    table = styled_table(title="Demo")
    table.add_column("col-a")
    table.add_column("col-b")
    table.add_row("alpha", "beta-value")

    console = Console(width=NON_TTY_WIDTH, file=io.StringIO())
    console.print(table)
    rendered = console.file.getvalue()

    assert "Demo" in rendered
    assert "col-a" in rendered and "col-b" in rendered
    assert "alpha" in rendered and "beta-value" in rendered


def test_get_console_widens_when_not_a_terminal(monkeypatch) -> None:
    """Off a terminal (and with COLUMNS unset) the console is widened so cells
    aren't truncated to the 80-col non-interactive default."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(output.Console, "is_terminal", property(lambda self: False))

    console = get_console()

    assert console.width == NON_TTY_WIDTH


def test_get_console_respects_columns_env(monkeypatch) -> None:
    """A user-set COLUMNS wins over the non-TTY widening."""
    monkeypatch.setenv("COLUMNS", "123")
    monkeypatch.setattr(output.Console, "is_terminal", property(lambda self: False))

    console = get_console()

    # COLUMNS is honored -> NOT forced to NON_TTY_WIDTH.
    assert console.width == 123


def test_is_tty_reflects_stdout(monkeypatch) -> None:
    """is_tty mirrors sys.stdout.isatty so live views can degrade off a TTY."""

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    class FakePipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdout", FakeTTY())
    assert is_tty() is True

    monkeypatch.setattr(sys, "stdout", FakePipe())
    assert is_tty() is False
