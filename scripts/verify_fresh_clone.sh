#!/usr/bin/env bash
# Rebuild the repo as a fresh clone would see it (from the git index) and
# check that it imports and that the tracked-files guard test works.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "step 1: stage guard test and this script"
git add tests/test_repo_tracked.py scripts/verify_fresh_clone.sh

echo "step 2: create temp dir"
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT

echo "step 3: export index to temp dir"
git checkout-index -a --prefix="$D/"

cd "$D"

echo "step 4: git init + uv sync"
git init -q
git add -A
uv sync

echo "step 5: import app.main"
DEMO_MODE=true uv run python -c "import app.main"

echo "step 6: guard test passes on clean tree"
uv run pytest tests/test_repo_tracked.py -q

echo "step 7: guard test fails with untracked probe module"
touch app/_untracked_probe.py
if uv run pytest tests/test_repo_tracked.py -q; then
    echo "guard test did not detect the untracked probe" >&2
    exit 1
fi

echo "fresh clone OK"
