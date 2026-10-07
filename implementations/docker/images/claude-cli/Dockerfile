# Agentic Primitives: Claude CLI Workspace Image
# Production-ready Docker image with Claude CLI for agent execution
#
# ⚠️  BUILD VIA: scripts/build-provider.py claude-cli
#     This Dockerfile expects a staged build context, not the raw repo.
#
# Features:
#   - Node.js 22 LTS (required for Claude CLI)
#   - Python 3.12 + uv (for hooks and agentic packages)
#   - Claude CLI with plugin-based hook loading via --plugin-dir (ADR-033)
#   - Pre-bundled plugins (sdlc, workspace) with agentic_events for observability
#   - LSP language servers (pyright, typescript-language-server, rust-analyzer)
#   - Security hardening (non-root, no setuid)
#
# Plugin Architecture (ADR-033):
#   Plugins are baked into /opt/agentic/plugins/ and loaded at runtime via
#   --plugin-dir flags built by the entrypoint. This is the same plugin format
#   used locally, ensuring identical behavior in both environments.
#   Plugin env vars declared in requires_env are auto-forwarded by the
#   agentic-isolation orchestrator.
#
# LSP Architecture:
#   The image bundles LSP binaries and pre-enables the official Claude Code LSP
#   plugins (pyright-lsp, typescript-lsp, rust-analyzer-lsp) in settings.json.
#   LSP servers are LAZY — they only start when Claude encounters files in the
#   matching language. An agent working on a Python-only repo will NOT start
#   rust-analyzer or typescript-language-server. This means the image is safe
#   for multi-agent orchestration: only the LSP servers actually needed per-task
#   consume memory at runtime.
#
# Events are emitted as JSONL to stderr, captured by the agent runner.
#
# See ADR-027: Provider-Based Workspace Images
# See ADR-033: Plugin-Native Workspace Images

# Global build args (declared before any FROM so all stages can inherit).
# Stages that need these must re-declare `ARG <name>` without a default to
# pull the value in. Bump deliberately — controls JSONL output contract,
# tool names (Agent vs Task), stream-json event schema, hook event names.
# See ClaudeToolName in agentic_events.types.
ARG CLAUDE_CLI_VERSION=2.1.126
# Codex CLI is installed alongside claude so a phase's primary agent can
# delegate one-shot to the other harness (opt-in via agent.allow_delegation).
ARG CODEX_CLI_VERSION=0.160.1
# Keep this aligned with Agentic Skills' validated installer contract.
ARG SKILLS_CLI_VERSION=1.7.0
# Playwright CLI for headless browser QA. Exact pin: the CLI version decides
# which browser revision it launches, and the image bakes exactly that one.
ARG PLAYWRIGHT_VERSION=1.63.0
# The archives that Playwright version downloads: revision, browser version
# and a sha256 per architecture, verified before unpacking. They move
# with PLAYWRIGHT_VERSION: the build checks them against that version's own
# install plan and fails if they disagree. How the digests were obtained is
# in the browser install layer below.
ARG PLAYWRIGHT_HEADLESS_SHELL_REVISION=1243
ARG PLAYWRIGHT_HEADLESS_SHELL_VERSION=153.0.8010.12
ARG PLAYWRIGHT_HEADLESS_SHELL_SHA256_AMD64=a9da028861a0cf789ff25c2fed45f5f1aaf969ed9247835b6a7821a4f7af9d1d
ARG PLAYWRIGHT_HEADLESS_SHELL_SHA256_ARM64=d433c45172c7836e38124fe545f767b02210bfb43a6262f08a297473a8e91c99
ARG PLAYWRIGHT_FFMPEG_REVISION=1011
ARG PLAYWRIGHT_FFMPEG_SHA256_AMD64=ebc74fc5b94830176a3c2914ae96bd8bc7f6a91f4f33890230f84a172ee61ccc
ARG PLAYWRIGHT_FFMPEG_SHA256_ARM64=2628c03f05318ff812c8c9baaf207dea2ddf53e818c0dc936714b0fbe3afb009

# =============================================================================
# Stage 1: Node.js base with system dependencies
# =============================================================================
FROM node:22-slim AS base

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies (Python is provided by uv, not apt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    jq \
    tree \
    procps \
    gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# =============================================================================
# Stage 2: Install package managers and Claude CLI
# =============================================================================
FROM base AS tooling

# Install uv (fast Python package + interpreter manager).
# Use the official uv image as a stage to copy the binary cleanly — avoids
# curl-piped installer and gives a reproducible source for the binary.
# Pin uv to a specific version — `:latest` would silently change between
# builds and is non-reproducible. Bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /usr/local/bin/

# Install Python via uv (replaces apt python3) and create a system venv
# at /opt/venv that all Python packages install into. World-readable so the
# non-root agent user can use it at runtime.
# Pin patch version so image's Python doesn't drift across rebuilds.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN uv python install 3.12.13 \
    && uv venv --python 3.12.13 /opt/venv \
    && chmod -R a+rX /opt/uv /opt/venv

# Login shells reset PATH in /etc/profile, dropping /opt/venv/bin. Codex runs
# its shell tool with `bash -lc`, so a syn-delegate started from Codex handed
# its child a PATH with no python3, and that child's capture hooks could not
# run (syntropic137#1398). Keep the venv first for login shells too.
RUN printf '%s\n' \
      '# Keep the image venv on PATH for login shells (agentic-workspace).' \
      'case ":$PATH:" in *":/opt/venv/bin:"*) ;; *) PATH="/opt/venv/bin:$PATH"; export PATH ;; esac' \
      > /etc/profile.d/10-agentic-venv.sh \
    && chmod 0644 /etc/profile.d/10-agentic-venv.sh

# Install Claude CLI globally via npm. Version pinned via global ARG above.
ARG CLAUDE_CLI_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}
# Install Codex CLI globally (delegation target). Pinned via global ARG above.
ARG CODEX_CLI_VERSION
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION}
RUN claude --version && codex --version

# Harness launch wrappers (syntropic137#1398). Hook commands run through a
# shell that reads BASH_ENV (bash) or ENV (interactive sh) before the command.
# An agent-writable file there could exit before the native child capture
# guard runs. Hooks inherit the harness process environment, which the agent
# cannot change after launch, so every claude/codex started by name starts
# without either variable. The npm symlinks are replaced by root-owned shims.
RUN for bin in claude codex; do \
      target="$(readlink -f "/usr/local/bin/${bin}")" \
      && rm "/usr/local/bin/${bin}" \
      && printf '%s\n' '#!/bin/sh' \
           '# Harness launch wrapper: no shell startup file reaches capture hooks.' \
           'unset BASH_ENV ENV' \
           "exec ${target} \"\$@\"" > "/usr/local/bin/${bin}" \
      && chmod 0755 "/usr/local/bin/${bin}" || exit 1; \
    done \
    && claude --version && codex --version

# Install the skills CLI globally via npm. Pin exact version deliberately.
ARG SKILLS_CLI_VERSION
RUN npm install -g skills@${SKILLS_CLI_VERSION} \
    && test "$(skills --version)" = "${SKILLS_CLI_VERSION}"

# =============================================================================
# Headless browser for UI QA (syntropic137/syntropic137#1028)
# =============================================================================
# WHY THIS IS HERE. Agents validate UI changes by rendering them and taking a
# screenshot. They run as uid 1000 with no root and no apt lists, so they
# cannot install Chromium's shared libraries (libnss3, libgbm1, libasound2,
# libatk1.0-0, libxkbcommon0, ...) at run time. A May probe needed a 40s
# .deb-extraction hack to get past that.
#
# WHAT. The Playwright CLI, exact-pinned and on PATH (so `npx playwright`
# resolves it with no network), and the Chromium headless shell only.
# `playwright screenshot` and every headless launch use the headless shell;
# full Chromium is for headed mode, which a workspace has no display for.
#
# WHY NOT --with-deps. It also installs Playwright's "tools" set (xvfb and
# CJK/emoji fonts), and xvfb drags in mesa and libllvm15: ~312 MiB of apt
# packages on bookworm against ~40 MiB for the list below, which is
# Playwright 1.63's own debian12 chromium list plus one Latin font so text
# renders. The ldd check after the browser install is what keeps this list
# honest: a Playwright bump that needs a library missing here fails the build
# instead of shipping a browser that cannot start.
#
# WHERE. A fixed, root-owned, world-readable PLAYWRIGHT_BROWSERS_PATH, not
# ~/.cache: $HOME is a tmpfs at run time and would hide anything baked there.
# A repository that depends on a DIFFERENT playwright version expects a
# different browser revision and will not find it here; pin to this version
# to reuse the baked browser.
#
# ASSERT THE VERSION, as for skills above, so a moved pin cannot ship a
# different CLI than the one whose browser revision was installed.
#
# WHY NOT `playwright install`. It downloads executables (the headless shell
# and ffmpeg, which `--only-shell chromium` also pulls in for video capture)
# and checks no digest. So the layer fetches the same archives itself and
# runs `sha256sum -c` BEFORE unpacking, as for `just`: a checksum taken after
# extraction has already run the risk it exists to prevent. It then lays them
# out exactly as Playwright would (<name>-<revision>/ under
# PLAYWRIGHT_BROWSERS_PATH plus the INSTALLATION_COMPLETE marker), so
# `npx playwright` finds them offline. Playwright's own
# `install --dry-run` plan is the reference: each pinned URL and directory
# must appear in it verbatim, so a PLAYWRIGHT_VERSION bump that moves the
# browser fails here until the pins move with it. The closing
# `playwright install`, pointed at a dead download host, writes Playwright's
# registry entry (.links/, which `install --list` and browser GC read) over
# the verified bytes; it fails the build rather than download anything
# those bytes did not already satisfy.
#
# WHERE THE DIGESTS COME FROM. Each archive was downloaded from the URL the
# dry-run plan names, and from a second, independent origin (Google's
# chrome-for-testing-public bucket for the shell; Playwright's Microsoft
# ESRP mirror for ffmpeg). Both copies hashed identically, and the shell
# archives also matched the MD5 Google's bucket publishes for them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    fonts-liberation \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ARG PLAYWRIGHT_VERSION
ARG PLAYWRIGHT_HEADLESS_SHELL_REVISION
ARG PLAYWRIGHT_HEADLESS_SHELL_VERSION
ARG PLAYWRIGHT_HEADLESS_SHELL_SHA256_AMD64
ARG PLAYWRIGHT_HEADLESS_SHELL_SHA256_ARM64
ARG PLAYWRIGHT_FFMPEG_REVISION
ARG PLAYWRIGHT_FFMPEG_SHA256_AMD64
ARG PLAYWRIGHT_FFMPEG_SHA256_ARM64
RUN set -eux; \
    npm install -g playwright@${PLAYWRIGHT_VERSION}; \
    test "$(playwright --version)" = "Version ${PLAYWRIGHT_VERSION}"; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) shell_platform="linux64"; ffmpeg_zip="ffmpeg-linux.zip"; \
             shell_sha256="${PLAYWRIGHT_HEADLESS_SHELL_SHA256_AMD64}"; \
             ffmpeg_sha256="${PLAYWRIGHT_FFMPEG_SHA256_AMD64}" ;; \
      arm64) shell_platform="linux-arm64"; ffmpeg_zip="ffmpeg-linux-arm64.zip"; \
             shell_sha256="${PLAYWRIGHT_HEADLESS_SHELL_SHA256_ARM64}"; \
             ffmpeg_sha256="${PLAYWRIGHT_FFMPEG_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture for the Playwright browser: ${arch}" >&2; exit 1 ;; \
    esac; \
    plan="$(playwright install --dry-run --only-shell chromium | tr -s ' ')"; \
    install_verified() { \
      dir="${PLAYWRIGHT_BROWSERS_PATH}/$1"; url="$2"; \
      printf '%s\n' "${plan}" | grep -qxF " Install location: ${dir}" \
        || { echo "playwright ${PLAYWRIGHT_VERSION} does not install to ${dir}" >&2; exit 1; }; \
      printf '%s\n' "${plan}" | grep -qxF " Download url: ${url}" \
        || { echo "playwright ${PLAYWRIGHT_VERSION} does not download ${url}" >&2; exit 1; }; \
      curl --fail --retry 3 -sSL -o /tmp/playwright-archive.zip "${url}"; \
      echo "$3  /tmp/playwright-archive.zip" | sha256sum -c -; \
      mkdir -p "${dir}"; \
      unzip -q /tmp/playwright-archive.zip -d "${dir}"; \
      rm /tmp/playwright-archive.zip; \
      touch "${dir}/INSTALLATION_COMPLETE"; \
    }; \
    install_verified "chromium_headless_shell-${PLAYWRIGHT_HEADLESS_SHELL_REVISION}" \
      "https://cdn.playwright.dev/builds/cft/${PLAYWRIGHT_HEADLESS_SHELL_VERSION}/${shell_platform}/chrome-headless-shell-${shell_platform}.zip" \
      "${shell_sha256}"; \
    install_verified "ffmpeg-${PLAYWRIGHT_FFMPEG_REVISION}" \
      "https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/${PLAYWRIGHT_FFMPEG_REVISION}/${ffmpeg_zip}" \
      "${ffmpeg_sha256}"; \
    PLAYWRIGHT_DOWNLOAD_HOST=http://127.0.0.1:9 playwright install --only-shell chromium; \
    chmod -R a+rX,go-w "${PLAYWRIGHT_BROWSERS_PATH}"; \
    shell="$(find "${PLAYWRIGHT_BROWSERS_PATH}" -type f -name chrome-headless-shell)"; \
    test -x "${shell}"; \
    if ldd "${shell}" | grep 'not found'; then exit 1; fi; \
    npm cache clean --force

# =============================================================================
# LSP: Language Servers for code intelligence
# Plugins: pyright-lsp, typescript-lsp, rust-analyzer-lsp (claude-plugins-official)
# =============================================================================

# TypeScript + Python LSP servers (via npm, same PATH as Claude CLI)
# Provides: typescript-language-server, pyright-langserver
RUN npm install -g typescript typescript-language-server pyright

# Rust: build deps for toolchain and cargo builds at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Rust toolchain + rust-analyzer
# Installed to /usr/local (not /home) to survive tmpfs mounts at runtime
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- \
    -y \
    --default-toolchain stable \
    --profile minimal \
    --no-modify-path \
    && rustup component add rust-analyzer \
    && chmod -R a+rX /usr/local/rustup /usr/local/cargo

# =============================================================================
# Stage 3: Install Python packages from staged wheels
# =============================================================================
FROM tooling AS packages

# Copy pre-built wheels from staged build context
COPY packages/*.whl /tmp/packages/

# Install agentic packages into /opt/venv (uv picks it up via VIRTUAL_ENV).
# pyjwt is required for Claude CLI MCP authentication
RUN uv pip install --no-cache /tmp/packages/*.whl pyjwt \
    && rm -rf /tmp/packages

# =============================================================================
# Stage 4: Final image with plugins and security
# =============================================================================
FROM packages AS final

# Create non-root user (node:22-slim already has node:node with uid/gid 1000)
# We'll use the existing 'node' user but rename home to /home/agent for clarity
RUN usermod -l agent -d /home/agent -m node \
    && groupmod -n agent node \
    && mkdir -p /home/agent \
    && chown -R agent:agent /home/agent

# Create directories
# ADR-036: Workspace structure convention
# - artifacts/input/  : Previous phase outputs (read-only, populated by framework)
# - artifacts/output/ : Current phase deliverables (agent writes here)
# - repos/            : Clone repositories here
RUN mkdir -p /workspace/artifacts/input \
    && mkdir -p /workspace/artifacts/output \
    && mkdir -p /workspace/repos \
    && mkdir -p /opt/agentic/plugins \
    && mkdir -p /opt/agentic/config \
    && mkdir -p /home/agent/.claude \
    && chown -R agent:agent /workspace \
    && chown -R agent:agent /opt/agentic \
    && chown -R agent:agent /home/agent/.claude

# NOTE: ~/.claude/settings.json is created by entrypoint.sh at runtime
# because /home/agent is a tmpfs mount that wipes anything baked into the image.

# Copy entrypoint script and workspace-shipped git hooks.
# git-hooks/ holds non-observability git hooks owned by the workspace itself
# (e.g. prepare-commit-msg for operator Co-authored-by attribution). These
# are NOT Claude Code plugins — the entrypoint installs them into the
# runtime git hooks directory alongside hooks contributed by plugins.
COPY workspace/entrypoint.sh /opt/agentic/entrypoint.sh
COPY scripts/git-hooks/ /opt/agentic/git-hooks/

# Copy pre-staged plugins from build context (ADR-033)
# Each plugin is a self-contained directory with .claude-plugin/plugin.json
# and hooks/hooks.json using ${CLAUDE_PLUGIN_ROOT} for path resolution.
COPY plugins/ /opt/agentic/plugins/

# Copy capability adapters (ADR-040)
# The /opt/agentic/capabilities/ tree holds one directory per registered
# capability (memory, session-store, ...), each with:
#   - doctor               — bash entry that exec's `python -m <pkg>.doctor`
#   - <provider>/init.sh   — env-var translation for each supported provider
#   - <provider>/doctor.sh — provider-specific health checks (optional)
# The Python packages themselves (agentic_memory, agentic_session_store) are
# installed into /opt/venv via the wheel staging in stage 3. Audit
# directories under /var/agentic/<capability>-doctor/ are created by the
# entrypoint at first use; orchestrators bind-mount them for durable audit
# logs.
COPY workspace/capabilities/ /opt/agentic/capabilities/

# Exporter provisioning contract (session-store capability):
# This image deliberately does NOT ship SeshMagicSessionExporter. The
# exporter is a reference client of the public APS-V1-0004 standard, not
# part of the private store server — vendoring one vendor's binary into
# this image would break the store being dependency-injected, and would
# require build credentials a turnkey user of this image does not have.
# The image defines the contract (see the session-store capability's
# `doctor`, whose `exporter_present` check looks for the binary on PATH);
# deployment supplies the binary via one of:
#   1. Bind-mount at container start: -v <host-path>:/usr/local/bin/SeshMagicSessionExporter:ro
#   2. A derived image that COPYs the binary in on top of this one.
#   3. A base-layer install baked into a custom variant of this image.
# When the exporter becomes public under AgentParadise, this block is
# replaced by a `curl` fetch and nothing else about the contract changes.
RUN mkdir -p /var/agentic \
    && chown -R agent:agent /var/agentic

# session-store spool root (ADR-040 / agentic_session_store's DEFAULT_SPOOL).
# The contract defaults AGENTIC_SESSION_STORE_SPOOL to /spool when unset;
# that default must exist and be writable by the non-root agent user for
# the capability to work without an operator explicitly bind-mounting
# something at /spool. Same rationale as /workspace, /opt/agentic,
# /var/agentic above.
RUN mkdir -p /spool \
    && chown -R agent:agent /spool

# Set permissions on plugins, entrypoint, capability adapters, and workspace-shipped hooks.
# Globbed rather than named individually so future entry scripts (e.g.
# finalize.sh) pick up the same permissions with no Dockerfile change.
RUN chmod -R 755 /opt/agentic/plugins /opt/agentic/git-hooks \
    && find /opt/agentic/plugins -name "*.py" -exec chmod 755 {} \; \
    && find /opt/agentic -maxdepth 1 -name "*.sh" -exec chmod 755 {} \; \
    && find /opt/agentic/capabilities -mindepth 2 -maxdepth 2 -name "doctor" -exec chmod 755 {} \; \
    && find /opt/agentic/capabilities -name "*.sh" -exec chmod 755 {} \; \
    && chown -R agent:agent /opt/agentic

# Verify core installations (before security hardening)
RUN claude --version && python3 --version && node --version && uv --version && gh --version

# Verify LSP server installations
RUN typescript-language-server --version \
    && pyright --version \
    && rust-analyzer --version \
    && rustc --version \
    && cargo --version

# Security: Remove setuid/setgid binaries (exclude /usr/local)
RUN find / -path /usr/local -prune -o -perm /6000 -type f -exec chmod a-s {} \; 2>/dev/null || true

# Set working directory
WORKDIR /workspace

# Switch to non-root user
USER agent

# Environment variables
ENV HOME=/home/agent \
    WORKSPACE_DIR=/workspace \
    # Plugins location (ADR-033)
    AGENTIC_PLUGINS_DIR=/opt/agentic/plugins \
    # Capability registry (ADR-040): space-separated list of capability
    # names the entrypoint's 5.6/5.7 loops iterate. A listed capability
    # with no AGENTIC_<CAP>_PROVIDER set is a silent no-op.
    AGENTIC_CAPABILITIES="memory session-store" \
    # Disable Claude Code attribution in commits/PRs
    ANTHROPIC_NO_ATTRIBUTION=1 \
    # Python lives in /opt/venv — installed and managed by uv (not apt).
    # VIRTUAL_ENV + PATH make the venv's python/pip the default.
    VIRTUAL_ENV=/opt/venv \
    # Rust toolchain paths
    # RUSTUP_HOME: read-only toolchain installed during build
    # CARGO_HOME: writable location for agent user (registry index, git checkouts)
    # /usr/local/cargo/bin stays on PATH for pre-installed binaries (cargo, rustc, rust-analyzer)
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/home/agent/.cargo \
    PATH=/usr/local/cargo/bin:$PATH

# Labels for version tracking and image selection.
# Re-declare ARG (no default) to pull value from the global ARG declared
# before any FROM — build args are stage-scoped and don't carry across.
ARG CLAUDE_CLI_VERSION
LABEL agentic.claude_cli_version=${CLAUDE_CLI_VERSION}
ARG CODEX_CLI_VERSION
LABEL agentic.codex_cli_version=${CODEX_CLI_VERSION}

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=2 \
    CMD [ "test", "-w", "/workspace" ]

# Entrypoint configures the workspace from env vars, then execs to CMD
# This runs AFTER tmpfs mounts, so configuration persists correctly
ENTRYPOINT ["/opt/agentic/entrypoint.sh"]

# Default command
CMD ["bash"]
