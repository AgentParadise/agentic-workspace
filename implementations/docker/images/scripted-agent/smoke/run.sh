#!/bin/bash
# Scripted-agent image smoke test.
#
# Usage: implementations/docker/images/scripted-agent/smoke/run.sh IMAGE [PLATFORM]
#   IMAGE     local tag or registry reference (tag or @sha256 digest)
#   PLATFORM  optional, e.g. linux/arm64 (runs under QEMU on an amd64 host)
#
# Runs check.py inside the image through its own entrypoint, as the agent user
# with --network none and a tmpfs $HOME as production runs it: claude and
# codex replay a tiny two-phase profile from the baked recordings, every model
# is reported as syn-scripted-agent, the artifacts are written, and a missing
# profile is refused, each phase leaves its native transcript and the baked
# apss-session-exporter archives both. See check.py for the full list. Then checks
# /opt/agentic/version.json (scripts/check-image-version-manifest.sh). Needs no
# network.
set -euo pipefail

image="${1:?usage: run.sh IMAGE [PLATFORM]}"
platform="${2:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

platform_args=()
if [ -n "$platform" ]; then
  platform_args=(--platform "$platform")
fi

# A multi-platform INDEX digest plus a platform: run the child manifest's own
# digest, so two platform smokes over one index do not collide in the classic
# overlay2 store ("cannot overwrite digest"). Same selector and rationale as
# toolchain/smoke/run.sh, which toolchain/smoke/test_platform_resolve.sh tests.
if [ -n "$platform" ] && [[ "$image" == *"@sha256:"* ]]; then
  repo="${image%@*}"
  if child="$(docker buildx imagetools inspect "$image" --format '{{json .Manifest}}' 2>/dev/null \
      | jq -er --arg p "$platform" '
          (.manifests // [])
          | map(select(
              .platform
              and (.platform.os + "/" + .platform.architecture) == $p
              and (.platform.os != "unknown")
            ))
          | if length == 1 then .[0].digest
            else error("expected exactly one \($p) manifest, found \(length)")
            end
        ')"; then
    echo "== resolved ${platform} manifest ${child} from index ${image##*@}"
    image="${repo}@${child}"
  else
    echo "== no distinct ${platform} manifest to resolve; using ${image} as given" >&2
  fi
fi

echo "== scripted-agent smoke: ${image} ${platform:-native}"

log="$(mktemp)"
trap 'rm -f "$log"' EXIT
if ! docker run --rm \
    "${platform_args[@]+"${platform_args[@]}"}" \
    --network none \
    --tmpfs "/home/agent:rw,exec,nosuid,size=128m,uid=1000,gid=1000" \
    -v "${here}:/opt/scripted-agent-smoke:ro" \
    "$image" /opt/scripted-agent/venv/bin/python /opt/scripted-agent-smoke/check.py 2>&1 | tee "$log"; then
  echo "== FAIL: smoke container exited non-zero" >&2
  exit 1
fi
grep -qx 'SCRIPTED AGENT SMOKE PASS' "$log" || { echo "== FAIL: no pass marker" >&2; exit 1; }

# The image version manifest (#93): present, parseable, and describing this
# image built from this checkout's commit, without the real harnesses.
"${here}/../../../../../scripts/check-image-version-manifest.sh" "$image" scripted-agent "$platform"

echo "== scripted-agent smoke PASS: ${image} ${platform:-native}"
