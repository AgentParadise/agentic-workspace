#!/bin/bash
# Buildfloor compile smoke test.
#
# Usage: implementations/docker/images/buildfloor/smoke/run.sh IMAGE [PLATFORM]
#   IMAGE     local tag or registry reference (tag or @sha256 digest)
#   PLATFORM  optional, e.g. linux/arm64 (runs under QEMU on an amd64 host)
#
# Proves, inside the image as production runs it (128 MB tmpfs $HOME, CPU
# quota), that a repository can:
#   - cargo build a crate whose rust-toolchain.toml pins a toolchain rustup
#     installs on first use,
#   - pnpm install through corepack with a hash-pinned packageManager,
#   - bun run a TypeScript entry that imports the installed package,
# and that the buildfloor entrypoint wrapper fails closed on a planted
# /workspace/.tools symlink. Needs network (toolchain, pnpm, npm registry).
set -euo pipefail

image="${1:?usage: run.sh IMAGE [PLATFORM]}"
platform="${2:-}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

platform_args=()
if [ -n "$platform" ]; then
  platform_args=(--platform "$platform")
fi

common=(
  --rm
  "${platform_args[@]+"${platform_args[@]}"}"
  --cpus 2
  --tmpfs "/home/agent:rw,exec,nosuid,size=128m,uid=1000,gid=1000"
)

echo "== buildfloor smoke: ${image} ${platform:-native}"

# 1. Positive path: compile, install, run.
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
if ! docker run "${common[@]}" \
    -v "${here}:/opt/buildfloor-smoke:ro" \
    "$image" bash /opt/buildfloor-smoke/inside.sh 2>&1 | tee "$log"; then
  echo "== FAIL: smoke container exited non-zero" >&2
  exit 1
fi
grep -qx 'BUILDFLOOR SMOKE PASS' "$log" || { echo "== FAIL: no pass marker" >&2; exit 1; }

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

echo "== buildfloor smoke PASS: ${image} ${platform:-native}"
