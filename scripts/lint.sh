#!/usr/bin/env bash
# Agent Keep — the single source of truth for the lint gate (issue #32). Runs the
# EXACT set of checks the ci.yml `lint` job runs, in one place, so BOTH the
# pre-push hook (.githooks/pre-push) and CI exercise identical commands and a red
# lint is caught locally before push instead of after a CI round-trip.
#
# Checks (mirror ci.yml's lint job verbatim):
#   1. uv run ruff check .
#   2. uv run ruff format --check .
#   3. shellcheck deploy.sh deploy/agent-keep-deploy scripts/*.sh
#
# All three ALWAYS run (we don't stop at the first failure) so the developer sees
# every problem at once; exit is non-zero if ANY check failed.
#
# ShellCheck parity: local and CI must run the SAME shellcheck version, or a
# finding can pass locally and fail in CI (that is exactly how stage 15's SC2015
# slipped through). The pinned version is 0.11.0 — see README.md. CI pins it by
# downloading that release; install the same one locally.
set -euo pipefail

# Resolve our own dir and the repo root so this works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

PINNED_SHELLCHECK_VERSION="0.11.0"

failed=()

run_check() {
  # run_check <label> <cmd...> — run a check, stream its output, record failure.
  local label="$1"
  shift
  echo "=== ${label} ==="
  echo "\$ $*"
  if "$@"; then
    echo "--- ${label}: PASS"
  else
    echo "--- ${label}: FAIL"
    failed+=("$label")
  fi
  echo
}

# Warn (don't fail) if the local shellcheck differs from the pinned CI version:
# a mismatch is the whole class of bug this stage exists to prevent.
if command -v shellcheck >/dev/null 2>&1; then
  local_sc="$(shellcheck --version 2>/dev/null | awk '/^version:/ {print $2}')"
  if [ -n "$local_sc" ] && [ "$local_sc" != "$PINNED_SHELLCHECK_VERSION" ]; then
    echo "WARNING: local shellcheck ${local_sc} != pinned ${PINNED_SHELLCHECK_VERSION};" \
         "findings may differ from CI. Install ${PINNED_SHELLCHECK_VERSION} for parity." >&2
    echo >&2
  fi
fi

run_check "ruff check"        uv run ruff check .
run_check "ruff format check" uv run ruff format --check .
run_check "shellcheck"        shellcheck deploy.sh deploy/agent-keep-deploy scripts/*.sh

# Terraform (stage 20 / ADR 0009): fmt-check + validate the provision/aws module.
# CI's lint job installs a pinned terraform (1.9.8) and ALWAYS runs these; locally
# we run them only when terraform is on PATH, and SKIP (don't fail) otherwise so a
# dev without terraform installed isn't blocked. `init -backend=false` + validate
# need no AWS creds, so this never touches (or bills) real AWS.
if command -v terraform >/dev/null 2>&1; then
  run_check "terraform fmt"      terraform -chdir=provision/aws fmt -check -recursive
  run_check "terraform init"     terraform -chdir=provision/aws init -backend=false -input=false
  run_check "terraform validate" terraform -chdir=provision/aws validate
else
  echo "=== terraform ==="
  echo "SKIP: terraform not on PATH; install 1.9.8 to run fmt-check + validate locally (CI always runs them)."
  echo
fi

if [ "${#failed[@]}" -ne 0 ]; then
  echo "LINT: FAIL (${#failed[@]} check(s) failed: ${failed[*]})"
  exit 1
fi

echo "LINT: PASS (all checks green)"
