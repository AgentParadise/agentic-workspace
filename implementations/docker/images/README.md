# Docker Workspace Images

Pre-configured Docker images for running AI agents in isolated environments.

## Available Images

| Image | Description | OTel Support |
|----------|-------------|--------------|
| `claude-cli` | Claude CLI with native OpenTelemetry (drives `claude -p` from the orchestrator) | ✅ Native |
| `interactive-tmux` | Claude + Codex + Gemini interactive CLIs in one tmux session, driven from host via `docker exec tmux send-keys`/`capture-pane`. Built for Max-plan subscription billing where `-p` is unavailable. See [provider README](./interactive-tmux/README.md) and `EXP-05-interactive-tmux-provider.md` in the private `AgentParadise/experiments` archive, under `agentic-primitives/2026-06-15--tmux-workspace--lab-reports/`. | ❌ (interactive TUIs do not emit the stream-json events `-p` mode produces; see provider README for the planned event-capture arc) |
| `omni-agent` | Claude and Codex on the shared capability runtime | ✅ |
| `buildfloor` | `omni-agent` plus a native build floor: build-essential, pkg-config, unzip, rustup (no toolchain; the repo's `rust-toolchain.toml` installs one on first use), pnpm via corepack, bun. Tool homes live under `/workspace/.tools`, cargo jobs follow the CPU quota. Built FROM an omni digest; compile smoke in [`buildfloor/smoke/`](./buildfloor/smoke/run.sh). | ✅ (inherited) |
| `base` | Minimal secure base (no agent) | N/A |

## Published Images

Release builds are published only from the protected `release` branch, signed
with cosign, and never tagged `latest`. See [`docs/RELEASE.md`](../../../docs/RELEASE.md).

| Image | GHCR package |
|---|---|
| `claude-cli` | `ghcr.io/agentparadise/agentic-workspace-claude` |
| `omni-agent` | `ghcr.io/agentparadise/agentic-workspace-omni-agent` |
| `buildfloor` | `ghcr.io/agentparadise/agentic-workspace-buildfloor` |

`interactive-tmux` and `base` are not published.

## Building Images

Use the build script to create workspace images:

```bash
# Build Claude CLI workspace
uv run scripts/build-provider.py claude-cli

# With custom tag
uv run scripts/build-provider.py claude-cli --tag myregistry/workspace:v1.0

# Without Docker cache
uv run scripts/build-provider.py claude-cli --no-cache

# Stage files only (for debugging)
uv run scripts/build-provider.py claude-cli --stage-only
```

## How It Works

The build process:

1. **Reads manifest** - `implementations/docker/images/<image>/manifest.yaml`
2. **Stages build context** - Creates `build/<provider>/` with:
   - Dockerfile
   - Plugins (from `plugins/`, per manifest `plugins.include`)
   - Python wheels (agentic_events)
3. **Builds Docker image** - Self-contained, reproducible

```
implementations/docker/images/claude-cli/
├── Dockerfile          # Image definition
└── manifest.yaml       # Build configuration

        │
        ▼ build-provider.py

build/claude-cli/       # Staged context
├── Dockerfile
├── plugins/
│   ├── sdlc/           # Self-contained plugin
│   └── workspace/      # Self-contained plugin
└── packages/
    └── *.whl
```

## Manifest Schema

```yaml
name: claude-cli
version: "1.0.0"
description: Claude CLI with native OTel

image:
  dockerfile: ./Dockerfile
  tag: agentic-workspace-claude-cli

plugins:
  include:
    - sdlc
    - workspace

defaults:
  allowed_tools: [Read, Write, Bash]
  otel_enabled: true

security:
  non_root: true
  no_setuid: true
```

## Adding a New Image

1. Create directory: `implementations/docker/images/<name>/`
2. Add `Dockerfile` with your agent runtime
3. Add `manifest.yaml` with configuration
4. Build: `uv run scripts/build-provider.py <name>`

## Security

All workspace images include:

- **Non-root user** (`agent:1000`)
- **No setuid/setgid binaries**
- **Read-only plugins directory**
- **Health checks**

See [ADR-027: Provider-Based Workspace Images](../../docs/adrs/027-provider-workspace-images.md)

## Modern Tooling

Images use:

- **bun** - Fast Node.js runtime + package manager
- **uv** - Fast Python package manager
