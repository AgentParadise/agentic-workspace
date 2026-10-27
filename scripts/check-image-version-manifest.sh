#!/bin/bash
# Check an image's /opt/agentic/version.json (#93).
#
# Usage: scripts/check-image-version-manifest.sh IMAGE PROVIDER [PLATFORM]
#   IMAGE     local tag or registry reference (resolve an index to its
#             per-platform child first when checking several platforms; see
#             toolchain/smoke/run.sh)
#   PROVIDER  claude-cli | omni-agent | toolchain | scripted-agent
#   PLATFORM  optional, e.g. linux/arm64
#
# Reads the file out of the image (no entrypoint, no network) and runs
# `version_manifest.py check` against it: the exact field set syntropic137
# parses, canonical bytes, this checkout's commit and commit time, the
# provider's manifest.yaml version and sha256, and the components the
# provider must (and must not) list.
set -euo pipefail

image="${1:?usage: check-image-version-manifest.sh IMAGE PROVIDER [PLATFORM]}"
provider="${2:?usage: check-image-version-manifest.sh IMAGE PROVIDER [PLATFORM]}"
platform="${3:-}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

platform_args=()
if [ -n "$platform" ]; then
  platform_args=(--platform "$platform")
fi

manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
if ! docker run --rm "${platform_args[@]+"${platform_args[@]}"}" --network none \
    --entrypoint cat "$image" /opt/agentic/version.json > "$manifest"; then
  echo "== FAIL: ${image} has no readable /opt/agentic/version.json" >&2
  exit 1
fi
cat "$manifest"
uv run --no-project "${root}/scripts/version_manifest.py" check "$provider" < "$manifest"
