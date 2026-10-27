# check=skip=InvalidDefaultArgInFrom
# =============================================================================
# Scripted-agent workspace image (load tests only)
# =============================================================================
#
# The claude-cli image with its harnesses replaced by the scripted-agent
# replayer (lib/python/scripted_agent), so syntropic137 can run N executions
# of a workflow without spending a token (#44, syntropic137#1717). Everything
# else claude-cli ships (entrypoint, capability runtime, git hooks, hardening)
# is inherited unchanged, so the load test exercises the same platform paths a
# real phase does.
#
# NEVER A PRODUCTION IMAGE. It is selected only by a load-test stack's
# SYN_WORKSPACE_IMAGE. Production image definitions are not touched here.
#
# ZERO TOKENS. The real claude and codex npm packages are uninstalled, not
# just shadowed, so nothing in the image can reach a model provider by path.
# Both names run the replayer, which opens no network connection; its git and
# gh side effects use the workspace's own credential against the fixture repo.
#
# BUILD. The context is the repository root (it needs lib/python and the
# claude-cli recordings), so build-provider.py does not stage it:
#
#   mapfile -t version_args < <(uv run --no-project scripts/version_manifest.py \
#     build-args scripted-agent --format docker)
#   docker build -f implementations/docker/images/scripted-agent/Dockerfile \
#     --build-arg CLAUDE_IMAGE=ghcr.io/agentparadise/agentic-workspace-claude@sha256:... \
#     "${version_args[@]}" .
#
# BASE PINNING. CLAUDE_IMAGE has no default on purpose, as in toolchain: an
# unset value fails the build instead of resolving a mutable tag. Pass a
# digest for anything published.
# =============================================================================

ARG CLAUDE_IMAGE
FROM ${CLAUDE_IMAGE}

USER root

# The install locations are scoped to this RUN, never ENV: an exported
# UV_PROJECT_ENVIRONMENT would make every fixture `uv sync` (the gates
# workload) target the replayer's environment instead of its own .venv. The
# shims below name the replayer's executable by path, so nothing at runtime
# needs these variables. --frozen, not --locked: the lock names the test-only
# ../agentic_isolation source, which is not copied, and --locked would read its
# metadata. tests/test_image_environment.py checks the lock in the full checkout.
COPY lib/python/scripted_agent /opt/scripted-agent/src
RUN cd /opt/scripted-agent/src \
    && UV_PYTHON_INSTALL_DIR=/opt/scripted-agent/python \
       UV_PROJECT_ENVIRONMENT=/opt/scripted-agent/venv \
       uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache/uv

# The recordings a profile's stream.recording may name.
COPY implementations/docker/images/claude-cli/fixtures/recordings \
     /opt/scripted-agent/recordings

# Replace the harnesses. Same launch wrapper shape as claude-cli: no shell
# startup file reaches anything the harness starts.
RUN npm uninstall -g @anthropic-ai/claude-code @openai/codex \
    && for bin in claude codex; do \
      rm -f "/usr/local/bin/${bin}" \
      && printf '%s\n' '#!/bin/sh' \
           '# Scripted-agent replayer: a load-test stand-in, no model is contacted.' \
           'unset BASH_ENV ENV' \
           'exec /opt/scripted-agent/venv/bin/scripted-agent "$@"' > "/usr/local/bin/${bin}" \
      && chmod 0755 "/usr/local/bin/${bin}" || exit 1; \
    done \
    && chmod -R a+rX /opt/scripted-agent \
    && ! find / -xdev -path /proc -prune -o -name cli.js -path '*claude-code*' -print | grep -q .

# =============================================================================
# Image version manifest: /opt/agentic/version.json (#93)
# =============================================================================
# What this image is, for syntropic137 (ImageManifest) and the E2B template's
# expected_manifest_hash. Written by scripts/version_manifest.py, bind-mounted
# for this RUN only, from the AGENTIC_* build args (exported to RUN as env) that
# `version_manifest.py build-args` computes (build-provider.py and the release
# workflow pass them; an unset one fails the build). Each component is checked
# against the binary on PATH before it is recorded. Deterministic per commit:
# built_at is the commit's time, never the wall clock. Carries claude-cli's components forward
# minus the two real harnesses this image uninstalled, and records the
# replayer's own package version instead.
ARG AGENTIC_PROVIDER
ARG AGENTIC_PROVIDER_VERSION
ARG AGENTIC_MANIFEST_DIGEST
ARG AGENTIC_BUILD_COMMIT
ARG AGENTIC_BUILT_AT
RUN --mount=type=bind,source=scripts/version_manifest.py,target=/tmp/version_manifest.py \
    /opt/venv/bin/python /tmp/version_manifest.py write \
      --inherit-from claude-cli \
      --drop claude_cli --drop codex_cli \
      --component "scripted_agent=$(/opt/scripted-agent/venv/bin/python -c 'import importlib.metadata as m; print(m.version("scripted-agent"))')"

USER agent
