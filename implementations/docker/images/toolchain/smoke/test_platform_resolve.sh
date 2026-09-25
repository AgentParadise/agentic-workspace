#!/bin/bash
# Tests the index -> child-manifest resolution that run.sh performs.
#
# Why this exists as its own test: the PR path calls run.sh with a LOCAL TAG
# and no platform argument, so the resolver in run.sh is never reached there.
# Without this, the resolver is first exercised by a push to `release`, where a
# mistake costs a failed publish. This runs the same selector against a real
# published index and asserts the whole decision, not just the happy path.
#
# What this does NOT test: the overlay2 graphdriver collision the resolver
# exists to avoid. That needs the classic image store and two sequential pulls
# of different platforms under one digest. Only the release path does that.
#
# Usage: test_platform_resolve.sh [INDEX_REF]
set -euo pipefail

# A published multi-platform index that also carries attestation manifests,
# which is the shape that matters: a loose selector picks an attestation.
INDEX="${1:-ghcr.io/agentparadise/agentic-workspace-omni-agent:v0.1.0}"

fail() { echo "FAIL: $*" >&2; exit 1; }

resolve() {
  docker buildx imagetools inspect "$1" --format '{{json .Manifest}}' \
    | jq -er --arg p "$2" '
        (.manifests // [])
        | map(select(
            .platform
            and (.platform.os + "/" + .platform.architecture) == $p
            and (.platform.os != "unknown")
          ))
        | if length == 1 then .[0].digest
          else error("expected exactly one \($p) manifest, found \(length)")
          end
      '
}

echo "== resolver test against ${INDEX}"

manifest_json="$(docker buildx imagetools inspect "$INDEX" --format '{{json .Manifest}}')"
total="$(jq -r '(.manifests // []) | length' <<<"$manifest_json")"
attestations="$(jq -r '[(.manifests // [])[] | select(.platform.os == "unknown")] | length' <<<"$manifest_json")"
echo "   index has ${total} manifests, ${attestations} of them attestations"
[ "$total" -gt 0 ] || fail "not an index, or empty: ${INDEX}"
[ "$attestations" -gt 0 ] \
  || fail "this index carries no attestation manifests, so it cannot prove they are excluded"

amd64="$(resolve "$INDEX" linux/amd64)" || fail "could not resolve linux/amd64"
arm64="$(resolve "$INDEX" linux/arm64)" || fail "could not resolve linux/arm64"
echo "   linux/amd64 -> ${amd64}"
echo "   linux/arm64 -> ${arm64}"

[ -n "$amd64" ] && [ -n "$arm64" ] || fail "empty digest resolved"
[ "$amd64" != "$arm64" ] \
  || fail "both platforms resolved to the SAME digest; the collision is not avoided"

index_digest="$(docker buildx imagetools inspect "$INDEX" --format '{{json .Manifest}}' | jq -r .digest)"
[ "$amd64" != "$index_digest" ] || fail "linux/amd64 resolved to the index digest, not a child"
[ "$arm64" != "$index_digest" ] || fail "linux/arm64 resolved to the index digest, not a child"

# Every resolved digest must be a real member of the index.
for d in "$amd64" "$arm64"; do
  jq -e --arg d "$d" '[(.manifests // [])[] | select(.digest == $d)] | length == 1' \
    <<<"$manifest_json" >/dev/null || fail "${d} is not a member of the index"
done

# No resolved digest may be an attestation.
for d in "$amd64" "$arm64"; do
  jq -e --arg d "$d" '
      [(.manifests // [])[] | select(.digest == $d and .platform.os == "unknown")] | length == 0
    ' <<<"$manifest_json" >/dev/null || fail "${d} is an attestation manifest"
done

# A platform that is not in the index must fail, not silently pass.
if resolve "$INDEX" linux/s390x >/dev/null 2>&1; then
  fail "linux/s390x resolved, but it is not in the index"
fi
echo "   linux/s390x correctly errors"

echo "== resolver test PASS"
