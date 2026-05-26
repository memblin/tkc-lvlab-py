# tkc-lvlab task runner. Run `just` (or `just default`) to list recipes.
#
# Unit / docs / build recipes are safe anywhere. The `integration*` recipes
# need a libvirt host (qemu:///system, the `default` network, /dev/kvm) and
# are gated by LVLAB_INTEGRATION=1 — never run them on a shared CI runner.

set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# List available recipes.
default:
    @just --list

# Unit tests on the current interpreter (integration tests excluded).
test:
    uv run pytest -m "not integration"

# Unit tests with coverage (terminal report).
test-cov:
    uv run pytest -m "not integration" --cov=tkc_lvlab --cov-report=term-missing

# Unit tests across the supported Python window (3.11–3.14).
test-matrix:
    for v in 3.11 3.12 3.13 3.14; do \
        echo "== python $v =="; \
        uv run -p $v pytest -m "not integration" -q; \
    done

# Integration-safety AST gate (every integration test must call assert_owned_by_test).
test-safety:
    uv run python tests/lint_test_safety.py

# Pre-commit hygiene (black, mdformat, shellcheck, yaml, eof/whitespace).
lint:
    uv run pre-commit run --all-files

# Build the docs site with strict warnings (CI-equivalent).
docs:
    uv run zensical build -s

# Serve the docs locally with live reload.
docs-serve:
    uv run zensical serve

# Build wheel + sdist into ./dist (version derived from the git tag).
build:
    uv build

# Build the wheel, then verify it installs + runs its scripts in a clean venv.
build-smoke:
    rm -rf dist .smoke-venv
    uv build
    uv venv .smoke-venv
    uv pip install --python .smoke-venv/bin/python dist/*.whl
    .smoke-venv/bin/lvlab --help > /dev/null
    .smoke-venv/bin/createvm --version
    .smoke-venv/bin/deletevm --version
    rm -rf .smoke-venv

# Full integration suite via LVLAB_INTEGRATION=1 (libvirt host; never in CI).
integration:
    LVLAB_INTEGRATION=1 uv run pytest -m integration -v

# createvm/deletevm matrix only; subset via LVLAB_TEST_DISTROS / LVLAB_TEST_MODES.
integration-createvm:
    LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_createvm.py -v

# Manifest-path smoke test: lvlab up/down/destroy + SSH-verify every machine in
# docs-extra/smoke/Lvlab.yml. Manual only — boots real qemu:///system VMs, never
# in CI. Run `lvlab init` in that dir first. Pass flags via ARGS, e.g.
# `just smoke ARGS="--format json --batch-size 4"`.
smoke args="":
    cd docs-extra/smoke && uv run lvlab smoke {{args}}
