"""Unit tests for the ``lvlab init`` CLI command.

These tests lock the per-image fetch+verify pipeline by mocking
:class:`tkc_lvlab.utils.images.CloudImage` at the cli import boundary
and asserting on (a) which methods were called and (b) status lines
emitted on stdout. They exist primarily to give the cognitive-
complexity refactor of ``init`` a safety net — coverage on the
command body was previously zero.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app
from tkc_lvlab.exceptions import ConfigError


def _mock_image(name: str, *, has_gpg: bool, has_checksum: bool) -> mock.MagicMock:
    """Build a CloudImage stand-in with realistic attribute shape."""
    img = mock.MagicMock()
    img.name = name
    img.image_fpath = f"/tmp/{name}/image.qcow2"
    img.image_url = f"https://example.invalid/{name}/image.qcow2"
    # ``filename`` is the URL basename on real CloudImage; the init path now
    # reads it (alongside ``image_url``) for the best-guess version column
    # (#124), so set a real string here — without this the MagicMock auto-attr
    # would crash re.match.
    img.filename = "image.qcow2"
    img.checksum_url_gpg = (
        f"https://example.invalid/{name}/keyring.gpg" if has_gpg else None
    )
    img.checksum_gpg_fpath = f"/tmp/{name}/keyring.gpg" if has_gpg else None
    img.checksum_url = (
        f"https://example.invalid/{name}/SHA256SUM" if has_checksum else None
    )
    img.checksum_fpath = f"/tmp/{name}/SHA256SUM" if has_checksum else None
    img.exists_locally.return_value = False
    img.download_image.return_value = True
    img.download_checksum.return_value = True
    img.download_checksum_gpg.return_value = True
    img.gpg_verify_checksum_file.return_value = True
    img.checksum_verify_image.return_value = True
    return img


def _run_init(images: list[mock.MagicMock]) -> "object":
    """Invoke ``lvlab init`` with ``parse_config`` and ``CloudImage`` patched."""
    runner = CliRunner()
    parse_return = (
        {"name": "test-env"},
        {f"img{idx}": {} for idx, _ in enumerate(images)},
        {},
        [],
    )
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(cli, "parse_config", return_value=parse_return)
        )
        stack.enter_context(mock.patch.object(cli, "CloudImage", side_effect=images))
        return runner.invoke(app, ["init"])


def test_init_downloads_all_artefacts_when_nothing_local() -> None:
    """Nothing on disk → every downloader runs; verifiers stay idle."""
    img = _mock_image("fedora44", has_gpg=True, has_checksum=True)
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_image.assert_called_once()
    img.download_checksum_gpg.assert_called_once()
    img.download_checksum.assert_called_once()
    img.gpg_verify_checksum_file.assert_not_called()
    img.checksum_verify_image.assert_not_called()
    assert "Initializing Libvirt Lab Environment: test-env" in result.output
    # Compact concurrent output (issue #104): a per-image completion line.
    assert "fedora44" in result.output
    assert "done" in result.output


def test_init_verifies_when_all_artefacts_already_local() -> None:
    """All on disk → no downloads; both verifiers fire and report OK."""
    img = _mock_image("fedora44", has_gpg=True, has_checksum=True)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_image.assert_not_called()
    img.download_checksum.assert_not_called()
    img.download_checksum_gpg.assert_not_called()
    img.gpg_verify_checksum_file.assert_called_once()
    img.checksum_verify_image.assert_called_once()
    # The verifiers fired (the behavioural contract); the compact #104 output
    # reports the image's completion rather than per-step status lines.
    assert "fedora44" in result.output
    assert "done" in result.output


def test_init_skips_gpg_branch_when_image_has_no_gpg_url() -> None:
    """Debian-style image: no checksum GPG keyring → skip GPG download/verify."""
    img = _mock_image("debian12", has_gpg=False, has_checksum=True)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_checksum_gpg.assert_not_called()
    img.gpg_verify_checksum_file.assert_not_called()
    img.checksum_verify_image.assert_called_once()
    assert "checksum GPG" not in result.output


def test_init_skips_checksum_branch_when_image_has_no_checksum_url() -> None:
    """Image without ``checksum_url`` → both checksum branches go quiet."""
    img = _mock_image("custom", has_gpg=False, has_checksum=False)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_checksum.assert_not_called()
    img.checksum_verify_image.assert_not_called()
    assert "checksum" not in result.output


def test_init_logs_error_when_image_download_fails() -> None:
    """``download_image`` returning False emits the error log line."""
    img = _mock_image("fedora44", has_gpg=False, has_checksum=False)
    img.download_image.return_value = False
    with mock.patch.object(cli, "logger") as mocked_logger:
        result = _run_init([img])

    assert result.exit_code == 0, result.output
    error_messages = [
        call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0]
        for call in mocked_logger.error.call_args_list
    ]
    assert any(
        "CloudImage download failed" in msg for msg in error_messages
    ), error_messages


def test_init_handles_parse_failure_via_typeerror() -> None:
    """``parse_config`` raising TypeError (missing-file unpack) → error + exit 1.

    The bare ``sys.exit()`` (exit 0) ``init`` used to call on a parse failure
    was standardized to ``typer.Exit(code=1)`` so the shell sees a nonzero
    status — matching every other lvlab subcommand.
    """
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")


def test_init_handles_parse_failure_via_configerror() -> None:
    """``parse_config`` raising ConfigError (bad structure) → error + exit 1."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=ConfigError("boom")),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    mocked_logger.error.assert_called_with("Could not parse config file.")


# ---------------------------------------------------------------------------
# No-manifest built-in default catalog (issue #97)
# ---------------------------------------------------------------------------


def test_init_with_no_manifest_initializes_builtin_defaults() -> None:
    """`lvlab init` with no Lvlab.yml downloads the built-in default catalog.

    Previously this errored (parse_config -> None -> exit 1), forcing users to
    `createvm --init-cloud-images`. Now it initializes BUILTIN_IMAGES.
    """
    from tkc_lvlab.utils.catalog import BUILTIN_IMAGES

    runner = CliRunner()
    images = [
        _mock_image(name, has_gpg=False, has_checksum=False) for name in BUILTIN_IMAGES
    ]
    with (
        mock.patch.object(cli, "parse_config", return_value=None),
        mock.patch.object(cli, "CloudImage", side_effect=images) as cloud_image_cls,
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    # One CloudImage per built-in, named for the catalog keys.
    assert cloud_image_cls.call_count == len(BUILTIN_IMAGES)
    built_names = [c.args[0] for c in cloud_image_cls.call_args_list]
    assert set(built_names) == set(BUILTIN_IMAGES)
    # Each image was fetched (not local in this test).
    for img in images:
        img.download_image.assert_called_once()


def test_init_with_no_manifest_still_fails_on_bad_manifest() -> None:
    """A structurally invalid manifest still fails loudly (not the None path)."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=ConfigError("boom")),
        mock.patch.object(cli, "CloudImage") as cloud_image_cls,
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    cloud_image_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrent init: progress state, cell rendering, failure isolation (#104)
# ---------------------------------------------------------------------------


def test_init_progress_state_snapshot_reflects_updates() -> None:
    """_InitProgress setters update state; snapshot returns a consistent copy."""
    progress = cli._InitProgress(["a", "b"])
    progress.set_bytes("a", 50, 100)  # set_bytes implies the downloading phase
    progress.set_phase("b", "verifying")

    snap = {s.name: s for s in progress.snapshot()}
    assert snap["a"].phase == "downloading"
    assert snap["a"].bytes_done == 50 and snap["a"].bytes_total == 100
    assert snap["b"].phase == "verifying"


def test_init_progress_cell_renders_bar_done_and_failed() -> None:
    """The compact cell shows a % bar while downloading, ✓ done, ✗ failed."""
    downloading = cli._ImageInitState(
        "x", phase="downloading", bytes_done=61, bytes_total=100
    )
    assert "%" in cli._init_progress_cell(downloading)
    assert "61%" in cli._init_progress_cell(downloading)

    done = cli._ImageInitState("x", phase="done")
    assert "✓" in cli._init_progress_cell(done)

    failed = cli._ImageInitState("x", phase="failed", error="HTTP 404")
    cell = cli._init_progress_cell(failed)
    assert "✗" in cell and "HTTP 404" in cell


# --- #124: best-guess image-version column in the init table ------------------


def test_init_progress_carries_version_when_supplied() -> None:
    """_InitProgress stamps each state with the version from the versions map (#124)."""
    progress = cli._InitProgress(
        ["a", "b"],
        versions={"a": "20260518-2482", "b": "noble"},
    )
    snap = {s.name: s for s in progress.snapshot()}
    assert snap["a"].version == "20260518-2482"
    assert snap["b"].version == "noble"


def test_init_progress_defaults_version_to_question_mark_when_unset() -> None:
    """A name absent from the versions map (or no map at all) gets ``?`` (#124)."""
    progress = cli._InitProgress(["a"])  # no versions map at all
    assert progress.snapshot()[0].version == "?"


def test_render_init_table_includes_a_version_column() -> None:
    """The init table grew a ``version`` column between ``image`` and ``phase`` (#124)."""
    states = [
        cli._ImageInitState("debian12", phase="done", version="20260518-2482"),
        cli._ImageInitState("ubuntu2404", phase="done", version="noble"),
    ]
    table = cli._render_init_table(states, env_name="default", jobs=2)
    headers = [col.header for col in table.columns]
    assert "version" in headers
    # version sits between image and phase for a scannable column order
    assert headers.index("version") < headers.index("phase")
    assert headers.index("image") < headers.index("version")


def test_render_init_table_renders_version_in_each_row() -> None:
    """Each rendered row carries the per-image version string (#124)."""
    from rich.console import Console

    states = [
        cli._ImageInitState("debian12", phase="done", version="20260518-2482"),
        cli._ImageInitState("ubuntu2404", phase="done", version="noble"),
    ]
    table = cli._render_init_table(states, env_name="default", jobs=2)

    console = Console(width=120, record=True, color_system=None)
    console.print(table)
    output = console.export_text()
    assert "20260518-2482" in output
    assert "noble" in output


def test_init_one_image_failure_exits_1_others_still_processed() -> None:
    """A fatal ImageError on one image fails init (exit 1) but doesn't wedge the rest."""
    from tkc_lvlab.exceptions import ImageError

    good = _mock_image("debian12", has_gpg=False, has_checksum=False)
    bad = _mock_image("fedora44", has_gpg=False, has_checksum=False)
    bad.download_image.side_effect = ImageError("could not download")

    result = _run_init([good, bad])

    assert result.exit_code == 1
    # The healthy image was still processed despite the other's failure.
    good.download_image.assert_called_once()
    bad.download_image.assert_called_once()
