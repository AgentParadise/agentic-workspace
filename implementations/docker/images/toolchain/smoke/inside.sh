#!/bin/bash
# Runs INSIDE a toolchain container, after the toolchain entrypoint wrapper
# and the shared entrypoint. Driven by run.sh; do not call directly.
#
# Expects: fixtures read-only at /opt/toolchain-smoke/fixtures, a 128 MB tmpfs
# $HOME (as production runs it), and --cpus 2.
set -euo pipefail

say() { printf '[smoke] %s\n' "$*"; }
fail() { printf '[smoke] FAIL: %s\n' "$*" >&2; exit 1; }

# --- Entrypoint wrapper contract ---------------------------------------------
for pair in .rustup:rustup .cargo:cargo .local/share/pnpm:pnpm .cache/node:node-cache .bun:bun .npm:npm-cache; do
  rel="${pair%%:*}"
  want="/workspace/.tools/${pair##*:}"
  got="$(readlink "$HOME/$rel" || true)"
  [ "$got" = "$want" ] || fail "$HOME/$rel -> '$got', want '$want'"
done
say "tool homes redirected to /workspace/.tools"

[ "$(stat -c %a /workspace/.tools)" = "700" ] || fail "/workspace/.tools is not mode 700"
grep -qx 'jobs = 2' "$HOME/.cargo/config.toml" || fail "cargo jobs not sized to --cpus 2: $(cat "$HOME/.cargo/config.toml" 2>&1)"
say "cargo jobs sized to the CPU quota (2)"

# The shared entrypoint still ran after the wrapper (it writes settings.json).
[ -f "$HOME/.claude/settings.json" ] || fail "shared entrypoint did not run (no ~/.claude/settings.json)"
say "shared entrypoint ran after the wrapper"

# --- Work on copies: fixtures are mounted read-only -------------------------
work=/workspace/smoke
rm -rf "$work"
mkdir -p "$work"
cp -r /opt/toolchain-smoke/fixtures/. "$work/"

# --- cargo: toolchain from rust-toolchain.toml, link with cc ----------------
cd "$work/rust-crate"
cargo build --locked --quiet
out="$(./target/debug/toolchain-smoke)"
[ "$out" = "toolchain-smoke rust-ok 42" ] || fail "cargo binary printed '$out'"
rustc --version | grep -q '^rustc 1\.90\.0 ' || fail "toolchain is not the pinned 1.90.0: $(rustc --version)"
[ -d /workspace/.tools/rustup/toolchains ] || fail "toolchain did not land under /workspace/.tools/rustup"
say "cargo build ok ($(rustc --version))"

# --- pnpm via corepack, pinned by packageManager hash -----------------------
cd "$work/js-package"
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0 npm_config_update_notifier=false
pnpm install --frozen-lockfile --reporter=silent
[ -f node_modules/ms/package.json ] || fail "pnpm install did not materialise ms"
[ "$(pnpm --version)" = "10.34.5" ] || fail "pnpm is not the packageManager pin: $(pnpm --version)"
say "pnpm install ok (pnpm $(pnpm --version))"

# --- bun -------------------------------------------------------------------
out="$(bun run index.ts)"
[ "$out" = "toolchain-smoke bun-ok 7200000" ] || fail "bun printed '$out'"
say "bun run ok (bun $(bun --version))"

# --- $HOME tmpfs stayed small ----------------------------------------------
used_kb="$(du -sk "$HOME" | cut -f1)"
[ "$used_kb" -lt 65536 ] || fail "\$HOME grew to ${used_kb} KB; tool homes leaked onto the tmpfs"
say "\$HOME usage ${used_kb} KB"

echo "TOOLCHAIN SMOKE PASS"
