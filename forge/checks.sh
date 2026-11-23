#!/usr/bin/env bash
# This repo's checks. Exit 0 only when a change is fit to review.
#
# The gate runs this file from the main checkout with the job's worktree as the
# working directory. Keep it fast, deterministic and non-interactive: no watch
# modes, no prompts, nothing that rewrites files. Put your real commands here:
#
#   npm ci --prefer-offline
#   npm run lint
#   npm test -- --run
#   npm run build
set -euo pipefail

echo "forge/checks.sh has no checks yet. Add this repo's test, lint, typecheck and build commands." >&2
exit 1
