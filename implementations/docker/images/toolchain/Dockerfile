# check=skip=InvalidDefaultArgInFrom
# =============================================================================
# Toolchain workspace image
# =============================================================================
#
# omni-agent plus a native toolchain. Everything omni ships (both harnesses,
# the capability runtime, plugins, security hardening) is inherited unchanged;
# this file only adds build tools and a toolchain-only entrypoint wrapper.
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
# Vulkan loader + Mesa lavapipe (software Vulkan on the CPU)
# =============================================================================
# Workspaces have no GPU. Repositories with GPU code (wgpu, ash) run their
# render-correctness tests on lavapipe, Mesa's CPU Vulkan driver, exactly as a
# GitHub ubuntu-latest runner does after
#   apt-get install mesa-vulkan-drivers libvulkan1 vulkan-tools
# (dream-ship-os render-correctness.yml). Its doctor checks all three packages
# by dpkg, the ICD at /usr/share/vulkan/icd.d/lvp_icd.json, and an llvmpipe
# adapter in `vulkaninfo --summary`, so vulkan-tools is required, not a nicety.
#
# WHY BACKPORTS ON BOOKWORM. Bookworm's own Mesa is 22.3 and installs the ICD
# arch-suffixed (lvp_icd.x86_64.json), which that doctor refuses. Backports
# carries Mesa 25.0, the same series ubuntu-latest runs, and names it
# lvp_icd.json. Matching the runner's lavapipe also matters for golden-image
# tests: a different rasteriser can differ by a pixel. Other bases install
# from their own archive; the ICD assertion below fails the build if that
# ever stops holding.
#
# KNOWN RISK, ACCEPTED. bookworm-backports stopped receiving updates on
# 2026-08-09 (https://backports.debian.org/Instructions/), so this Mesa 25.0.7
# and libdrm 2.4.123 are frozen: no further security fixes. Accepted because
# stock 22.3 fails the doctor and DreamShip's goldens match Mesa 25, and the
# exposure is narrow: a CPU-only Vulkan driver in a hardened, sandboxed
# workspace, running only the repository's own shaders.
# TODO(#111): move the omni/toolchain base to trixie (Mesa 25.0 with
# lvp_icd.json in main) and delete this backports block.
#
# SCOPE OF BACKPORTS. Never `-t bookworm-backports`: that raises the whole
# suite to 990 and lets any dependency come from it. Instead the suite is
# pinned to -1 except an allowlist at 600 (above main's 500): the three
# requested packages plus exactly the backported dependencies apt needs.
# mesa-vulkan-drivers 25.0.7-2~bpo12+1 Depends libdrm2 and libdrm-amdgpu1
# (>= 2.4.121; bookworm has 2.4.114), and libdrm2 2.4.123-1~bpo12+1 Depends
# libdrm-common (>= itself). Everything else (libllvm15, libz3-4, the xcb
# and wayland libs, libvulkan1, vulkan-tools) resolves from bookworm main;
# libvulkan1 and vulkan-tools have no backport and are listed only so the
# allowlist names what was asked for. Source list and pin are removed in the
# same layer, and the installed Mesa must be 25.x or the build fails, so a
# silent fallback to 22.3 cannot ship.
#
# Size: ~205 MiB installed (mesa-vulkan-drivers 65, libllvm15 115, libz3 23),
# ~42 MiB compressed. Here and not in omni, for the same reason as the C
# toolchain: omni consumers do not compile or render.
RUN set -eux; \
    . /etc/os-release; \
    if [ "${VERSION_CODENAME}" = bookworm ]; then \
      test -f /usr/share/keyrings/debian-archive-keyring.gpg; \
      echo "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian bookworm-backports main" \
        > /etc/apt/sources.list.d/bookworm-backports.list; \
      printf '%s\n' \
        'Package: *' \
        'Pin: release n=bookworm-backports' \
        'Pin-Priority: -1' \
        '' \
        'Package: libvulkan1 mesa-vulkan-drivers vulkan-tools libdrm2 libdrm-amdgpu1 libdrm-common' \
        'Pin: release n=bookworm-backports' \
        'Pin-Priority: 600' \
        > /etc/apt/preferences.d/bookworm-backports-vulkan; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      libvulkan1 \
      mesa-vulkan-drivers \
      vulkan-tools; \
    rm -f /etc/apt/sources.list.d/bookworm-backports.list \
          /etc/apt/preferences.d/bookworm-backports-vulkan; \
    rm -rf /var/lib/apt/lists/*; \
    apt-get clean; \
    if [ "${VERSION_CODENAME}" = bookworm ]; then \
      mesa_version="$(dpkg-query -W -f='${Version}' mesa-vulkan-drivers)"; \
      case "${mesa_version}" in \
        25.*~bpo12*) ;; \
        *) echo "mesa-vulkan-drivers ${mesa_version} is not the bookworm-backports 25.x build" >&2; exit 1 ;; \
      esac; \
    fi; \
    test -f /usr/share/vulkan/icd.d/lvp_icd.json; \
    command -v vulkaninfo

# =============================================================================
# rustup (binary + proxies, NO toolchain)
# =============================================================================
# A repository's own rust-toolchain.toml selects and installs its pinned
# toolchain on first use, into RUSTUP_HOME under /workspace (see
# scripts/toolchain-entrypoint.sh). rustup-init comes by URL and checksum
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
# Toolchain-only entrypoint wrapper
# =============================================================================
# Redirects the tool homes that outgrow the $HOME tmpfs into /workspace/.tools
# and sizes cargo's job count to the container CPU quota, then execs the
# shared /opt/agentic/entrypoint.sh unchanged. Living in a wrapper (not in the
# shared entrypoint) is what keeps omni-agent and claude-cli behaviour
# byte-for-byte identical.
COPY scripts/toolchain-entrypoint.sh /opt/agentic/toolchain-entrypoint.sh
RUN chmod 755 /opt/agentic/toolchain-entrypoint.sh \
    && bash -n /opt/agentic/toolchain-entrypoint.sh

# Verify everything omni verified still resolves, plus the toolchain.
RUN claude --version && codex --version && python3 --version && node --version \
    && uv --version && gh --version && just --version && make --version \
    && cc --version && c++ --version && pkg-config --version \
    && rustup --version && test -L /usr/local/bin/pnpm && bun --version \
    && test -f /usr/share/vulkan/icd.d/lvp_icd.json && command -v vulkaninfo

# Re-apply omni's setuid/setgid strip: the packages above ran as root and could
# have introduced new ones.
RUN find / -path /usr/local -prune -o -path /proc -prune -o -perm /6000 -type f -exec chmod a-s {} \; 2>/dev/null || true

# Give $HOME back to agent, then PROVE it.
#
# Everything above ran as root while HOME stayed /home/agent (inherited from
# omni; this image never overrides it). Anything that touched $HOME therefore
# left root-owned paths behind. The verification step above has two such
# writers, both measured: `gh --version` writes a device id under
# $XDG_STATE_HOME/gh, creating /home/agent/.local, and `codex --version`
# creates /home/agent/.codex/tmp. Both landed owned root:root.
#
# Note the shape of this: the offenders are --version calls, the cheapest
# possible commands, in a step whose entire purpose is to prove the image is
# healthy. Any binary may touch $HOME on startup, so the rule cannot be "audit
# each command" - it has to be "hand $HOME back afterwards, and verify".
#
# At runtime the 128 MB tmpfs over /home/agent masks the damage, so production
# never saw it - and the smoke suite mounts that same tmpfs "as production runs
# it", so it could not see it either. What does see it is a bare `docker run`,
# which is how a consumer probes a pinned digest: the entrypoint cannot
# mkdir ~/.local/share, and EVERY harness probe fails with a permission error
# that names none of this.
#
# The assertion is the point. A chown alone fixes today's instance; the test
# makes the next root-section write fail the build instead of shipping a latent
# image. Any image that adds `USER root` needs this pair before `USER agent`.
# `set -eu` and the temp file are deliberate. Piping find into `grep -q` would
# report only GREP's status, so a find that errors part way (and prints nothing)
# would look identical to a clean home and pass the gate silently. Under -e a
# failing find aborts the build instead, which is the fail-closed direction.
RUN chown -R agent:agent /home/agent \
    && set -eu; \
       bad=/tmp/home-ownership-violations; \
       find /home/agent ! -user agent -printf '  %u %p\n' > "$bad"; \
       if [ -s "$bad" ]; then \
         echo "ERROR: root-owned paths remain under /home/agent:" >&2; \
         cat "$bad" >&2; \
         exit 1; \
       fi; \
       rm -f "$bad"

USER agent
WORKDIR /workspace

LABEL agentic.toolchain.rustup_version=${RUSTUP_VERSION} \
      agentic.toolchain.bun_version=${BUN_VERSION}

# Setting ENTRYPOINT resets the inherited CMD, so both are restated.
ENTRYPOINT ["/opt/agentic/toolchain-entrypoint.sh"]
CMD ["bash"]
