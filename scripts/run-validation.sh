#!/usr/bin/env bash
# Run the unit + integration test loop for host validation, capture a
# single paste-back-ready output block, and tee it to results/<host>.txt.
#
# Designed to pair with scripts/host-bootstrap.sh: bootstrap a fresh
# host, then run this from a repo checkout to produce the canonical
# validation record for that host.
#
# Usage:
#   scripts/run-validation.sh [output-dir]
#
# The script:
#   - Identifies host distro / kernel / system Python / git SHA.
#   - Runs the unit suite (no coverage — this is a validation pass).
#   - Runs the integration suite (LVLAB_INTEGRATION=1, no coverage).
#   - Surfaces any per-URI skip reasons from the integration run.
#   - Tees everything to scripts/results/<distro>-<version>-<git-sha>.txt.
#   - Exits non-zero if either pytest run fails.
#
# The output block is the canonical artifact to paste back into the
# validation tracking record — see docs-extra/host-validation.md.

set -uo pipefail

err() { printf 'error: %s\n' "$*" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
    err "uv not found on PATH — did you run scripts/host-bootstrap.sh and re-login? Try: export PATH=\"\${HOME}/.local/bin:\${PATH}\""
fi

if [[ ! -r /etc/os-release ]]; then
    err "/etc/os-release missing — cannot identify host"
fi

# shellcheck disable=SC1091
source /etc/os-release

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || err "not inside a git repo — cd into a tkc-lvlab-py checkout first"
cd "${repo_root}" || err "could not cd into repo root: ${repo_root}"

git_sha="$(git rev-parse --short HEAD)"
distro_slug="${ID}-${VERSION_ID//./_}"
output_dir="${1:-${repo_root}/scripts/results}"
mkdir -p "${output_dir}"
output_file="${output_dir}/${distro_slug}-${git_sha}.txt"

# Buffer all output, then tee to file + stdout at the end so the user
# sees the full block at once and the saved file is identical.
tmp_out="$(mktemp)"
trap 'rm -f "${tmp_out}"' EXIT

{
    printf '================================================================\n'
    printf 'lvlab host validation results\n'
    printf '================================================================\n'
    printf 'Distro:         %s\n' "${PRETTY_NAME}"
    printf 'Kernel:         %s\n' "$(uname -r)"
    printf 'System Python:  %s\n' "$(python3 --version 2>&1)"
    printf 'uv:             %s\n' "$(uv --version 2>&1)"
    printf 'Git SHA:        %s\n' "${git_sha}"
    printf 'Git branch:     %s\n' "$(git rev-parse --abbrev-ref HEAD)"
    printf 'Run at:         %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'Host:           %s\n' "$(hostname)"
    printf 'User:           %s\n' "${USER}"
    printf '\n'

    printf -- '----------------------------------------------------------------\n'
    printf -- 'Unit tests: uv run pytest -q\n'
    printf -- '----------------------------------------------------------------\n'
    if uv run pytest -q 2>&1; then
        unit_status=PASS
    else
        unit_status=FAIL
    fi
    printf '\n'

    printf -- '----------------------------------------------------------------\n'
    printf -- 'Integration tests: LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py -v\n'
    printf -- '----------------------------------------------------------------\n'
    if LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py -v 2>&1; then
        integ_status=PASS
    else
        integ_status=FAIL
    fi
    printf '\n'

    printf -- '----------------------------------------------------------------\n'
    printf 'Result: unit=%s integration=%s\n' "${unit_status}" "${integ_status}"
    printf -- '----------------------------------------------------------------\n'
    if [[ "${unit_status}" == "PASS" && "${integ_status}" == "PASS" ]]; then
        printf 'OVERALL: PASS\n'
    else
        printf 'OVERALL: FAIL\n'
    fi
    printf '================================================================\n'
} 2>&1 | tee "${tmp_out}"

# Atomic move to final path so partial files can't masquerade as a
# completed run.
mv "${tmp_out}" "${output_file}"
# tmp_out is now gone; clear the trap.
trap - EXIT

printf '\nResults saved to: %s\n' "${output_file}" >&2

# Exit non-zero if either suite failed.
if grep -q '^OVERALL: FAIL$' "${output_file}"; then
    exit 1
fi
