# check=skip=InvalidDefaultArgInFrom
# =============================================================================
# Buildfloor workspace image
# =============================================================================
#
# omni-agent plus a native build floor. Everything omni ships (both harnesses,
# the capability runtime, plugins, security hardening) is inherited unchanged;
# this file only adds build tools and a buildfloor-only entrypoint wrapper.
#
# WHY A SEPARATE IMAGE. omni-agent deliberately ships no compiler (see its
# Dockerfile header). Repositories that compile native code cannot run their
# own gates there, and the agent cannot fix that at runtime: it runs as uid
# 1000 without sudo. Measured inside a real workspace against a Rust + pnpm
# monorepo (dream-ship_v0), 2026-09-17:
#
#   cargo check  ->  error: linker `cc` not found
#   bun install  ->  error: unzip is required to install bun
#   pnpm         ->  command not found; `corepack enable` -> EROFS on
#                    /usr/local/bin
#
# Keeping those tools here, not in omni, leaves omni's size and behaviour
# exactly as before for every consumer that does not compile.
#
# BASE PINNING. OMNI_IMAGE has no default here on purpose: an unset value
# fails the build ("base name should not be blank") instead of silently
# resolving a mutable tag. scripts/build-provider.py passes the manifest's
# local default (omni-agent-workspace:latest); the release workflow passes the
# exact ghcr.io/agentparadise/agentic-workspace-omni-agent@sha256:... digest it
# pushed and verified earlier in the same run.
# =============================================================================

# rustup binary only, no toolchain. Bump deliberately, and move the two SHA256
# pins below with it (they are the published rustup-init.sha256 values).
ARG RUSTUP_VERSION=1.29.1
# bun runtime. Same bump rule: the two SHA256 pins come from the release's
# SHASUMS256.txt.
ARG BUN_VERSION=1.3.14
ARG OMNI_IMAGE

FROM ${OMNI_IMAGE}

USER root

# =============================================================================
# C toolchain and unzip
# =============================================================================
# omni already has make. build-essential adds gcc/g++/libc6-dev, which cargo
# needs as its linker and which Python sdists and node-gyp need too. unzip is
# what bun's own installer and many npm postinstalls expect.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      pkg-config \
      unzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# =============================================================================
# rustup (binary + proxies, NO toolchain)
# =============================================================================
# A repository's own rust-toolchain.toml selects and installs its pinned
# toolchain on first use, into RUSTUP_HOME under /workspace (see
# scripts/buildfloor-entrypoint.sh). rustup-init comes by URL and checksum
# because a releases URL is not content-addressed: the SHA256 pins the bytes.
#
# rustup dispatches on argv[0], so cargo, rustc and friends are symlinks to the
# one binary. That keeps them on PATH while every mutable byte (toolchains,
# registry, git checkouts) goes to the runtime-writable homes.
ARG RUSTUP_VERSION
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) rustup_target="x86_64-unknown-linux-gnu"; \
             rustup_sha256="dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71" ;; \
      arm64) rustup_target="aarch64-unknown-linux-gnu"; \
             rustup_sha256="15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433" ;; \
      *) echo "unsupported architecture for rustup: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${rustup_target}/rustup-init"; \
    echo "${rustup_sha256}  /tmp/rustup-init" | sha256sum -c -; \
    install -m 755 /tmp/rustup-init /usr/local/bin/rustup; \
    rm -f /tmp/rustup-init; \
    for proxy in cargo rustc rustdoc rustfmt cargo-fmt cargo-clippy clippy-driver rust-analyzer rust-gdb rust-lldb; do \
      ln -s rustup "/usr/local/bin/${proxy}"; \
    done; \
    rustup --version 2>/dev/null | grep -q "^rustup ${RUSTUP_VERSION} "; \
    cc --version; \
    unzip -v | head -1

# =============================================================================
# pnpm via corepack
# =============================================================================
# Only pnpm is enabled: the node base already ships yarn 1 at
# /usr/local/bin/yarn, and a blanket `corepack enable` would replace it. The
# shim resolves the version a repository pins in packageManager and downloads
# it into COREPACK_HOME (redirected under /workspace) at first use.
RUN corepack enable pnpm && test -L /usr/local/bin/pnpm

# =============================================================================
# bun
# =============================================================================
# Baked rather than left to the agent: the only agent-side install path puts a
# ~90 MB binary in ~/.bun, on the 128 MB $HOME tmpfs. Pinned by version and by
# SHA256, checked before unpacking.
ARG BUN_VERSION
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) bun_target="linux-x64"; \
             bun_sha256="951ee2aee855f08595aeec6225226a298d3fea83a3dcd6465c09cbccdf7e848f" ;; \
      arm64) bun_target="linux-aarch64"; \
             bun_sha256="a27ffb63a8310375836e0d6f668ae17fa8d8d18b88c37c821c65331973a19a3b" ;; \
      *) echo "unsupported architecture for bun: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o "/tmp/bun-${bun_target}.zip" \
      "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-${bun_target}.zip"; \
    echo "${bun_sha256}  /tmp/bun-${bun_target}.zip" | sha256sum -c -; \
    unzip -q "/tmp/bun-${bun_target}.zip" -d /tmp/bun-unpack; \
    install -m 755 "/tmp/bun-unpack/bun-${bun_target}/bun" /usr/local/bin/bun; \
    rm -rf "/tmp/bun-${bun_target}.zip" /tmp/bun-unpack; \
    test "$(bun --version)" = "${BUN_VERSION}"
# Bun's transpiler cache is pure overhead in a workspace that lives for one
# phase; Bun's own container guidance disables it.
ENV BUN_RUNTIME_TRANSPILER_CACHE_PATH=0

# =============================================================================
# Buildfloor-only entrypoint wrapper
# =============================================================================
# Redirects the tool homes that outgrow the $HOME tmpfs into /workspace/.tools
# and sizes cargo's job count to the container CPU quota, then execs the
# shared /opt/agentic/entrypoint.sh unchanged. Living in a wrapper (not in the
# shared entrypoint) is what keeps omni-agent and claude-cli behaviour
# byte-for-byte identical.
COPY scripts/buildfloor-entrypoint.sh /opt/agentic/buildfloor-entrypoint.sh
RUN chmod 755 /opt/agentic/buildfloor-entrypoint.sh \
    && bash -n /opt/agentic/buildfloor-entrypoint.sh

# Verify everything omni verified still resolves, plus the build floor.
RUN claude --version && codex --version && python3 --version && node --version \
    && uv --version && gh --version && just --version && make --version \
    && cc --version && c++ --version && pkg-config --version \
    && rustup --version && test -L /usr/local/bin/pnpm && bun --version

# Re-apply omni's setuid/setgid strip: the packages above ran as root and could
# have introduced new ones.
RUN find / -path /usr/local -prune -o -path /proc -prune -o -perm /6000 -type f -exec chmod a-s {} \; 2>/dev/null || true

USER agent
WORKDIR /workspace

LABEL agentic.buildfloor.rustup_version=${RUSTUP_VERSION} \
      agentic.buildfloor.bun_version=${BUN_VERSION}

# Setting ENTRYPOINT resets the inherited CMD, so both are restated.
ENTRYPOINT ["/opt/agentic/buildfloor-entrypoint.sh"]
CMD ["bash"]
