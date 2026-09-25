#!/bin/bash
# Toolchain compile smoke test.
#
# Usage: implementations/docker/images/toolchain/smoke/run.sh IMAGE [PLATFORM]
#   IMAGE     local tag or registry reference (tag or @sha256 digest)
#   PLATFORM  optional, e.g. linux/arm64 (runs under QEMU on an amd64 host)
#
# Proves, inside the image as production runs it (128 MB tmpfs $HOME, CPU
# quota), that a repository can:
#   - cargo build a crate whose rust-toolchain.toml pins a toolchain rustup
#     installs on first use,
#   - pnpm install through corepack with a hash-pinned packageManager,
#   - bun run a TypeScript entry that imports the installed package,
# and that the toolchain entrypoint wrapper fails closed on a planted
# /workspace/.tools symlink. Needs network (toolchain, pnpm, npm registry).
set -euo pipefail

image="${1:?usage: run.sh IMAGE [PLATFORM]}"
platform="${2:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

platform_args=()
if [ -n "$platform" ]; then
  platform_args=(--platform "$platform")
fi

# When the reference is a multi-platform INDEX digest and a platform is asked
# for, run the child manifest's own digest instead of the index digest.
#
# The classic overlay2 graphdriver stores one platform per image digest. Two
# smokes over the same index digest therefore collide: the first pulls its
# platform under that digest, the second tries to pull a different platform
# under the same digest and docker refuses with
#
#     docker: cannot overwrite digest sha256:...
#
# which reads like a corrupt image and is really a store limitation. The
# containerd snapshotter holds several platforms per digest, so this never
# reproduces on a developer machine that has it enabled - only in CI.
#
# Resolving the child digest keeps the test honest: each child manifest is a
# member of the index that gets signed and tagged, so what is tested is still
# part of what ships. --platform is left in place; it agrees with the resolved
# manifest and still selects QEMU emulation.
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
    # Not an index, or the platform is absent from it. Leave the reference
    # alone: a single-platform digest has no conflict to avoid, and a genuinely
    # missing platform must fail in docker run with a clear message rather than
    # be silently skipped here.
    echo "== no distinct ${platform} manifest to resolve; using ${image} as given" >&2
  fi
fi

common=(
  --rm
  "${platform_args[@]+"${platform_args[@]}"}"
  --cpus 2
  --tmpfs "/home/agent:rw,exec,nosuid,size=128m,uid=1000,gid=1000"
)

echo "== toolchain smoke: ${image} ${platform:-native}"

# 1. Positive path: compile, install, run.
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
if ! docker run "${common[@]}" \
    -v "${here}:/opt/toolchain-smoke:ro" \
    "$image" bash /opt/toolchain-smoke/inside.sh 2>&1 | tee "$log"; then
  echo "== FAIL: smoke container exited non-zero" >&2
  exit 1
fi
grep -qx 'TOOLCHAIN SMOKE PASS' "$log" || { echo "== FAIL: no pass marker" >&2; exit 1; }

# 2. Negative path: a planted /workspace/.tools symlink must stop the
#    container before the shared entrypoint or the command runs.
planted="$(mktemp -d)"
trap 'rm -f "$log"; rm -rf "$planted"' EXIT
# mktemp -d is mode 700 and owned by the host user; uid 1000 in the container
# must be able to see the symlink, or the run fails for the wrong reason.
chmod 755 "$planted"
ln -s /tmp "${planted}/.tools"
set +e
neg_out="$(docker run "${common[@]}" -v "${planted}:/workspace" "$image" \
  bash -c 'echo COMMAND_RAN' 2>&1)"
neg_rc=$?
set -e
if [ "$neg_rc" -eq 0 ] || grep -q COMMAND_RAN <<<"$neg_out" \
   || ! grep -q 'refusing unsafe tool-home root' <<<"$neg_out"; then
  echo "== FAIL: planted .tools symlink was not refused (rc=${neg_rc}):" >&2
  echo "$neg_out" >&2
  exit 1
fi
echo "[smoke] planted /workspace/.tools symlink refused (rc=${neg_rc})"

echo "== toolchain smoke PASS: ${image} ${platform:-native}"
