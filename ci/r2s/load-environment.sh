#!/usr/bin/env bash
set -euo pipefail
root=$(realpath "$(dirname "$0")/../..")
[[ $root == "${GITHUB_WORKSPACE:?}" ]]
cd "$root"
python3 -B ci/r2s/pipeline.py import environment
zstd -dc _ci/inbox/environment/builder.tar.zst | docker load
python3 -B ci/r2s/pipeline.py check-environment
image=$(python3 -B -c 'import json; print(json.load(open("_ci/state/context.json"))["container_id"])')
printf 'BUILDER_IMAGE=%s\n' "$image" >> "$GITHUB_ENV"
