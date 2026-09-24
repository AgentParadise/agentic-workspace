#!/bin/bash
# Print the tags one release build of one image may carry, one per line.
#
# Usage: release-image-tags.sh IMAGE MANIFEST_VERSION REPO_VERSION COMMIT_SHA
#
#   <commit sha>        always; the run FAILS if it already exists, because a
#                       rebuild of one commit is not byte-identical and a sha
#                       tag must never move
#   <manifest version>  only if that tag does not exist yet
#   v<repo version>     only if that tag does not exist yet
#
# Version tags are first-write-wins: a later release never moves them onto
# different bytes. A build whose version already exists is still published
# under its commit sha, and a warning says to bump the version.
#
# There is deliberately no `latest`. Consumers pin digests; a floating tag is
# only a way to pull something nobody reviewed the digest of.
set -euo pipefail

image="${1:?image}"
manifest_version="${2:?manifest version}"
repo_version="${3:?repo version}"
sha="${4:?commit sha}"

semver='^[0-9]+\.[0-9]+\.[0-9]+$'
[[ "$manifest_version" =~ $semver ]] || { echo "::error::manifest version is not X.Y.Z: $manifest_version" >&2; exit 1; }
[[ "$repo_version" =~ $semver ]] || { echo "::error::repo version is not X.Y.Z: $repo_version" >&2; exit 1; }
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::commit sha is not 40 hex chars: $sha" >&2; exit 1; }

# 0 = exists, 1 = absent. Any other registry answer is an error: treating a
# transient failure as "absent" would move a version tag.
tag_exists() {
  local out
  if out="$(docker buildx imagetools inspect "${image}:$1" 2>&1)"; then
    return 0
  fi
  if grep -qE ': not found$' <<<"$out"; then
    return 1
  fi
  echo "::error::cannot tell whether ${image}:$1 exists: $out" >&2
  exit 1
}

# Run BEFORE the build, so a rerun of an already-published commit stops before
# pushing or signing anything.
if tag_exists "$sha"; then
  echo "::error::${image}:${sha} already exists; a sha tag is immutable. Land a new commit on release to publish again." >&2
  exit 1
fi
tags=("${image}:${sha}")
for t in "$manifest_version" "v${repo_version}"; do
  if tag_exists "$t"; then
    echo "::warning::${image}:${t} is already published and is left in place (version tags are first-write-wins). Bump the version to tag this build; it is still published as :${sha}." >&2
  else
    tags+=("${image}:${t}")
  fi
done

for t in "${tags[@]}"; do
  if [[ "$t" == *:latest ]]; then
    echo "::error::refusing to publish a latest tag: $t" >&2
    exit 1
  fi
done

printf '%s\n' "${tags[@]}"
