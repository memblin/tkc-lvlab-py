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


def test_get_console_max_width_caps_a_wide_console(monkeypatch) -> None:
    """``max_width`` caps the rendered width (so a wide table wraps at ~80)."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(output.Console, "is_terminal", property(lambda self: False))

    # Off a terminal the console would widen to NON_TTY_WIDTH (200); the cap
    # brings it down to 80.
    assert get_console(max_width=80).width == 80


def test_get_console_max_width_does_not_upscale_a_small_window(monkeypatch) -> None:
    """A window narrower than ``max_width`` keeps its (smaller) width."""
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setattr(output.Console, "is_terminal", property(lambda self: True))

    assert get_console(max_width=80).width == 60


def test_set_no_color_disables_console_color(monkeypatch) -> None:
    """``set_no_color(True)`` makes ``get_console`` return a no-colour console."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    try:
        output.set_no_color(True)
        assert output.color_disabled() is True
        console = get_console()
        assert console.no_color is True
        # Colour system fully disabled -> no bold/attribute escapes either, so
        # output is genuinely plain even on a terminal (issue #131).
        assert console.color_system is None
    finally:
        output.set_no_color(False)
    assert output.color_disabled() is False


def test_color_disabled_honors_no_color_env(monkeypatch) -> None:
    """The NO_COLOR env var disables colour even without the flag."""
    output.set_no_color(False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert output.color_disabled() is True
    assert get_console().no_color is True


def test_secho_forces_color_false_when_disabled(monkeypatch) -> None:
    """With colour disabled, ``secho`` pins ``color=False`` so click/typer strip
    ANSI even on a TTY — Click ignores ``NO_COLOR`` on its own (issue #131)."""
    captured: dict = {}
    monkeypatch.setattr(
        output.typer, "secho", lambda msg=None, **kw: captured.update(msg=msg, kw=kw)
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    output.set_no_color(True)
    try:
        output.secho("boom", fg="red", err=True)
    finally:
        output.set_no_color(False)
    assert captured["kw"]["color"] is False
    # Forwards the caller's own styling/stream kwargs untouched.
    assert captured["kw"]["fg"] == "red" and captured["kw"]["err"] is True


def test_secho_leaves_color_to_click_when_enabled(monkeypatch) -> None:
    """With colour enabled, ``secho`` does not pin ``color`` — Click auto-detects
    (ANSI on a TTY, plain when piped), so default behaviour is unchanged."""
    captured: dict = {}
    monkeypatch.setattr(
        output.typer, "secho", lambda msg=None, **kw: captured.update(msg=msg, kw=kw)
    )
    monkeypatch.delenv("NO_COLOR", raising=False)
    output.set_no_color(False)
    output.secho("ok", fg="green")
    assert "color" not in captured["kw"]


def test_secho_honors_no_color_env(monkeypatch) -> None:
    """The ``NO_COLOR`` env var alone (no ``--no-color`` flag) also strips."""
    captured: dict = {}
    monkeypatch.setattr(
        output.typer, "secho", lambda msg=None, **kw: captured.update(msg=msg, kw=kw)
    )
    output.set_no_color(False)
    monkeypatch.setenv("NO_COLOR", "1")
    output.secho("x")
    assert captured["kw"]["color"] is False


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
