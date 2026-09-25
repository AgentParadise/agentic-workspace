#!/bin/bash
# =============================================================================
# Toolchain entrypoint wrapper
# =============================================================================
#
# Runs ONLY in the toolchain image, before the shared workspace entrypoint,
# then execs it with the original arguments. The shared
# /opt/agentic/entrypoint.sh (workspace/entrypoint.sh) is not modified, so
# omni-agent and claude-cli behave exactly as they did.
#
# It does two things:
#   1. Redirect tool homes that outgrow the 128 MB $HOME tmpfs to
#      /workspace/.tools, via symlinks at the default paths.
#   2. Size cargo's job count to the container CPU quota.
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# 1. Tool homes under /workspace
# -----------------------------------------------------------------------------
# WHY. /home/agent is a 128 MB tmpfs. rustup toolchains (~1.6 GB for a pinned
# nightly), cargo's registry, pnpm's content store and corepack's downloaded
# package managers all default into $HOME, and each of them filled it in a
# measured workspace run (2026-09-17: ENOSPC from pnpm, corepack and rustup).
# /workspace is on the container filesystem, with room and exec permission.
#
# WHY SYMLINKS AND NOT ONLY ENV. Task runners that sanitise the environment
# drop cache variables: turbo's strict env mode stripped RUSTUP_HOME in the
# same run, and rustup fell back to ~/.rustup and filled the tmpfs. A symlink
# at the default path holds no matter what the environment says.
#
# An existing path is left alone (a caller that mounted one meant it).
TOOL_HOMES_ROOT=/workspace/.tools

# Fail closed on a planted path. /workspace is usually a bind mount, so
# anything that wrote into it before this ran (a reused directory, a hostile
# checkout) could have left `.tools` or one of its children as a symlink
# pointing somewhere else - back onto this tmpfs, or into a directory that is
# later uploaded. mkdir -p would follow it silently. Refuse instead: a
# workspace that starts with a symlinked tool home is not one to run in.
_unsafe_tool_path() {
    [ -L "$1" ] || { [ -e "$1" ] && [ ! -d "$1" ]; }
}
if _unsafe_tool_path "$TOOL_HOMES_ROOT"; then
    echo "[toolchain-entrypoint] refusing unsafe tool-home root (symlink or non-directory): $TOOL_HOMES_ROOT" >&2
    exit 1
fi
mkdir -p "$TOOL_HOMES_ROOT"
chmod 700 "$TOOL_HOMES_ROOT"

# default path under $HOME : directory under $TOOL_HOMES_ROOT
#   rustup toolchains, downloads, tmp      ~/.rustup
#   cargo registry, git, .package-cache    ~/.cargo
#   pnpm store / cache / state             ~/.local/share/pnpm, ~/.cache/pnpm, ~/.local/state/pnpm
#   corepack's downloaded package managers ~/.cache/node/corepack
#   node-gyp headers (new and old layouts) ~/.cache/node-gyp, ~/.node-gyp
#   npm cache                              ~/.npm
#   uv cache and uv-managed Pythons        ~/.cache/uv, ~/.local/share/uv
#   pip cache                              ~/.cache/pip
#   bun install cache, globals and bins    ~/.bun
for link in \
    .rustup:rustup \
    .cargo:cargo \
    .local/share/pnpm:pnpm \
    .cache/pnpm:pnpm-cache \
    .local/state/pnpm:pnpm-state \
    .cache/node:node-cache \
    .cache/node-gyp:node-gyp \
    .node-gyp:node-gyp-legacy \
    .npm:npm-cache \
    .cache/uv:uv-cache \
    .local/share/uv:uv-data \
    .cache/pip:pip-cache \
    .bun:bun; do
    rel="${link%%:*}"
    target="${TOOL_HOMES_ROOT}/${link##*:}"
    if _unsafe_tool_path "$target"; then
        echo "[toolchain-entrypoint] refusing unsafe tool-home target (symlink or non-directory): $target" >&2
        exit 1
    fi
    mkdir -p "$target"
    if [ ! -e "$HOME/$rel" ] && [ ! -L "$HOME/$rel" ]; then
        mkdir -p "$(dirname "$HOME/$rel")"
        ln -s "$target" "$HOME/$rel"
    fi
done
unset -f _unsafe_tool_path
unset link rel target

# -----------------------------------------------------------------------------
# 2. Size cargo's parallelism to the container, not the host
# -----------------------------------------------------------------------------
# WHY. Workspaces run with a CPU quota (2 CPUs on the syntropic137 selfhost)
# and a 4 GB memory limit, but nproc - and therefore cargo's default job
# count - reports every host core (16 there). A wgpu test build then ran 16
# rustc and linker processes at once and the kernel OOM-killed ld (measured
# 2026-09-17: `collect2: fatal error: ld terminated with signal 9 [Killed]`).
#
# WHY A CONFIG FILE AND NOT CARGO_BUILD_JOBS. Task runners that sanitise the
# environment drop the variable before cargo sees it; cargo always reads
# $CARGO_HOME/config.toml. An existing file is left alone, and an explicit
# CARGO_BUILD_JOBS still wins, because cargo ranks env over config.
_cargo_config="$HOME/.cargo/config.toml"
if [ ! -e "$_cargo_config" ] && [ ! -L "$_cargo_config" ]; then
    _jobs=""
    _quota=""
    _period=""
    if [ -r /sys/fs/cgroup/cpu.max ]; then
        # cgroup v2: "<quota|max> <period>"
        read -r _quota _period < /sys/fs/cgroup/cpu.max || true
    else
        # cgroup v1: quota is -1 when unlimited, which the numeric check rejects
        for _cg in /sys/fs/cgroup/cpu /sys/fs/cgroup/cpu,cpuacct; do
            if [ -r "$_cg/cpu.cfs_quota_us" ] && [ -r "$_cg/cpu.cfs_period_us" ]; then
                read -r _quota < "$_cg/cpu.cfs_quota_us" || true
                read -r _period < "$_cg/cpu.cfs_period_us" || true
                break
            fi
        done
        unset _cg
    fi
    # Only plain positive integers reach arithmetic: bash evaluates variable
    # contents recursively inside $(( )), so "max", "-1" or anything odd is
    # rejected here rather than interpreted.
    case "$_quota" in ''|*[!0-9]*) _quota="" ;; esac
    case "$_period" in ''|*[!0-9]*) _period="" ;; esac
    if [ -n "$_quota" ] && [ -n "$_period" ] && [ "$_period" -gt 0 ]; then
        _jobs=$(( (_quota + _period - 1) / _period ))
    fi
    if [ -n "$_jobs" ] && [ "$_jobs" -ge 1 ] 2>/dev/null; then
        printf '# Written by the toolchain entrypoint: jobs sized to the container CPU quota.\n[build]\njobs = %s\n' "$_jobs" > "$_cargo_config"
    fi
    unset _jobs _quota _period
fi
unset _cargo_config

# -----------------------------------------------------------------------------
# 3. Hand over to the shared workspace entrypoint, unchanged
# -----------------------------------------------------------------------------
exec /opt/agentic/entrypoint.sh "$@"
