"""Static safety lint for the integration test suite.

Every integration test that creates
libvirt/qemu state must declare which resource names it owns, via at
least one ``assert_owned_by_test(...)`` call. The runtime guard in
``tests/conftest.py`` (the same function) refuses to operate on any
name that doesn't carry the per-session ``LVLAB_TEST_PREFIX``; this
static guard catches a missing-call regression at PR-review time,
without needing ``LVLAB_INTEGRATION=1`` to be set.

The check is intentionally narrow: it does NOT try to verify that every
destructive subprocess in the test body has a matching guard, because
the tests run real ``lvlab destroy --force`` / ``deletevm --force``
subprocesses that are themselves constrained by the
``make_test_name`` -> prefix invariant. The point of this lint is to
make the absence of *any* guard impossible to miss.

Run:

    uv run python tests/lint_test_safety.py

The script exits 0 when every ``@pytest.mark.integration`` function in
``tests/test_integration_*.py`` calls ``assert_owned_by_test`` at least
once. Otherwise it prints the offending function(s) and exits 1.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

GUARD_FUNCTION_NAME = "assert_owned_by_test"
"""The function call name a static-safe integration test must contain."""

INTEGRATION_TEST_GLOB = "test_integration_*.py"
"""File-name pattern restricting this lint's blast radius.

Unit tests don't touch real libvirt state — only ``test_integration_*``
files do — so the guard requirement applies only there.
"""


def _has_assert_owned_by_test_call(node: ast.AST) -> bool:
    """Return True iff ``node`` contains a top-level call to the guard.

    Walks the AST under ``node`` and matches an :class:`ast.Call` whose
    callee is a bare :class:`ast.Name` with id ``assert_owned_by_test``.
    A qualified ``module.assert_owned_by_test(...)`` would not match;
    the integration tests import the helper by bare name, so this is
    the form we lint for.

    Args:
        node: Any AST node — typically a function body.

    Returns:
        True if at least one matching call is present anywhere in the
        subtree.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name) and func.id == GUARD_FUNCTION_NAME:
            return True
    return False


def _is_integration_marked(node: ast.FunctionDef) -> bool:
    """Return True iff ``node`` carries the ``@pytest.mark.integration`` decorator.

    Recognises the canonical form ``@pytest.mark.integration`` (attribute
    access ending in ``integration``). A bare ``@integration`` would not
    match — the integration tests in this repo do not use that form, so
    catching it would be premature.

    Args:
        node: A function definition AST node.

    Returns:
        True when the decorator chain ends in ``.integration``.
    """
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr == "integration":
            return True
        if isinstance(dec, ast.Call):
            target = dec.func
            if isinstance(target, ast.Attribute) and target.attr == "integration":
                return True
    return False


def _collect_failures(tests_dir: Path) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, function_name)`` for every integration test missing the guard.

    Args:
        tests_dir: Directory holding the integration-test modules
            (typically ``Path("tests")``).

    Returns:
        A list of failure rows; empty list means the lint passed.
    """
    failures: list[tuple[Path, int, str]] = []
    for test_file in sorted(tests_dir.glob(INTEGRATION_TEST_GLOB)):
        tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not _is_integration_marked(node):
                continue
            if not _has_assert_owned_by_test_call(node):
                failures.append((test_file, node.lineno, node.name))
    return failures


def main() -> int:
    """Run the lint and exit with the right status.

    Returns:
        ``0`` when every integration test calls the guard at least once;
        ``1`` (printed to stderr with the offending list) otherwise.
    """
    tests_dir = Path(__file__).resolve().parent
    failures = _collect_failures(tests_dir)
    if not failures:
        return 0
    print(
        f"Integration-test safety lint failed: {len(failures)} function(s) "
        f"missing the required {GUARD_FUNCTION_NAME!r} call.",
        file=sys.stderr,
    )
    for path, lineno, name in failures:
        print(
            f"  {path}:{lineno}: {name} is @pytest.mark.integration but never "
            f"calls {GUARD_FUNCTION_NAME}() — every integration test must "
            f"declare the test-owned resource names it operates on.",
            file=sys.stderr,
        )
    print(
        "\nRun `assert_owned_by_test(<name>)` (imported from "
        "tests.conftest) on each libvirt domain / on-disk path your "
        "test will create. See tests/conftest.py for the runtime guard.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
