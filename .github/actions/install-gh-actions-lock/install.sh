#!/usr/bin/env bash
# Installs the github/gh-actions-lock gh extension from its release binary,
# checked against its SHA-256. The action beside this runs it, and so do the
# downstream-bump and downstream-push actions, which run it by path, as a
# composite action can't use a local action of its own repository when
# another repository uses it.
set -euo pipefail

GH_ACTIONS_LOCK_VERSION=v0.4.1
GH_ACTIONS_LOCK_SHA256=39deb2fe714dbf74b073d8245fc7f3d50cd0eee18d5c0900e9e849ae933ae17e

dir="$RUNNER_TEMP/gh-actions-lock"
mkdir -p "$dir"
curl -fsSL -o "$dir/gh-actions-lock" \
  "https://github.com/github/gh-actions-lock/releases/download/$GH_ACTIONS_LOCK_VERSION/linux-amd64"
echo "$GH_ACTIONS_LOCK_SHA256  $dir/gh-actions-lock" | sha256sum --check --strict
chmod +x "$dir/gh-actions-lock"
cd "$dir"
gh extension install .
