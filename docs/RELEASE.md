# Workspace image release

## Images

| Image | Source | Built from |
|---|---|---|
| `ghcr.io/agentparadise/agentic-workspace-claude` | `implementations/docker/images/claude-cli` | source |
| `ghcr.io/agentparadise/agentic-workspace-omni-agent` | `implementations/docker/images/omni-agent` | source |
| `ghcr.io/agentparadise/agentic-workspace-buildfloor` | `implementations/docker/images/buildfloor` | the omni-agent digest pushed in the same run |

These names are distinct from every Agentic Primitives package, so the two
repositories never publish to one package. Note the claude-cli image is
`agentic-workspace-claude`, not `agentic-workspace-claude-cli`: that name is
AP's existing package (linked to AgentParadise/agentic-primitives), as is
`omni-agent-workspace`.

## Trust root: the protected `release` branch

`.github/workflows/release-images.yml` publishes and signs images **only** on
a push to `release`. `release` accepts changes only by pull request from
`main`, with required checks, and no force push. Nothing else publishes:
pushes to `main`, tags, GitHub release events, pull requests and manual
dispatches never push or sign.

Consumers verify the exact identity:

```bash
cosign verify \
  --certificate-identity \
    'https://github.com/AgentParadise/agentic-workspace/.github/workflows/release-images.yml@refs/heads/release' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/agentparadise/agentic-workspace-buildfloor@sha256:REPLACE_ME
```

Renaming or moving the workflow file changes that identity and breaks every
consumer's verification.

## Cutting a release

1. Land changes on `main` as usual. Bump the manifest `version` of every
   image whose contents changed, and `[workspace.package] version` in
   `Cargo.toml` for the repository release.
2. Open a pull request from `main` into `release`. `CI` and the release
   workflow's `Integration Gate` run on it; nothing is published.
3. Merge it. The push to `release` runs:
   1. **Integration gate**: single-arch builds of all three images, the
      entrypoint integration suite, and the amd64 buildfloor compile smoke.
   2. **claude-cli** and **omni-agent** in parallel: multi-arch build, push
      untagged by digest with BuildKit SBOM + max-mode provenance, keyless
      cosign sign, verify against the identity above, then tag.
   3. **buildfloor**, after omni: verify omni's signature, build FROM
      `agentic-workspace-omni-agent@<that digest>`, push untagged by digest,
      run the compile smoke (`cargo build` with a `rust-toolchain.toml` pin,
      `pnpm install`, `bun run`) on **linux/amd64 and linux/arm64** against
      that digest, then sign, verify and tag. What was tested is byte for byte
      what is tagged.

## Tags

- `<commit sha>` (40 hex): every release build.
- `<manifest version>` (for example `1.7.1`) and `v<repo version>` (for
  example `v0.2.0`): only the first time that tag is published. Version tags
  are first-write-wins; a later build never moves them. If a version already
  exists, the build is still published under its commit sha and the run
  warns to bump the version.
- **No `latest`.** It is never published.

Labels on every release image include `agentic.image.channel=release`,
`org.opencontainers.image.revision=<commit>`, and for buildfloor
`org.opencontainers.image.base.name` / `base.digest` naming the omni digest it
was built on.

## Consuming

Never promote a tag. Copy the digest and the identity above into one
reviewable consumer PR (Syntropic137 `PINNED_DIGESTS` plus its cosign identity
regex), then run the rollback proof against the previous digest.

## Local builds

```bash
uv run scripts/build-provider.py omni-agent        # tags omni-agent-workspace:latest locally
uv run scripts/build-provider.py buildfloor        # FROM omni-agent-workspace:latest
uv run scripts/build-provider.py buildfloor --build-arg OMNI_IMAGE=<ref>   # any other base
implementations/docker/images/buildfloor/smoke/run.sh agentic-workspace-buildfloor:latest
```

Local tags such as `:latest` exist only in your Docker daemon; they are what
the integration tests resolve by default and are never pushed.
