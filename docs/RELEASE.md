# Workspace image release

## Images

| Image | Source | Built from |
|---|---|---|
| `ghcr.io/agentparadise/agentic-workspace-claude` | `implementations/docker/images/claude-cli` | source |
| `ghcr.io/agentparadise/agentic-workspace-omni-agent` | `implementations/docker/images/omni-agent` | source |
| `ghcr.io/agentparadise/agentic-workspace-toolchain` | `implementations/docker/images/toolchain` | the omni-agent digest pushed in the same run |
| `ghcr.io/agentparadise/agentic-workspace-scripted-agent` | `implementations/docker/images/scripted-agent` | the claude-cli digest pushed in the same run (load tests only, never a production image) |

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
  ghcr.io/agentparadise/agentic-workspace-toolchain@sha256:REPLACE_ME
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
   1. **Integration gate**: single-arch builds of all four images, the
      `version.json` check on each, the entrypoint integration suite, the
      amd64 toolchain compile smoke and the amd64 scripted-agent replay smoke.
   2. **claude-cli** and **omni-agent** in parallel: multi-arch build, push
      untagged by digest with BuildKit SBOM + max-mode provenance, keyless
      cosign sign, verify against the identity above, then tag.
   3. **toolchain**, after omni: verify omni's signature, build FROM
      `agentic-workspace-omni-agent@<that digest>`, push untagged by digest,
      run the compile smoke (`cargo build` with a `rust-toolchain.toml` pin,
      `pnpm install`, `bun run`) on **linux/amd64 and linux/arm64** against
      that digest, then sign, verify and tag. What was tested is byte for byte
      what is tagged.
   4. **scripted-agent**, after claude-cli: verify claude-cli's signature,
      build FROM `agentic-workspace-claude@<that digest>` with the repository
      root as context, push untagged by digest, run the replay smoke
      (`claude` and `codex` replay a two-phase profile from the baked
      recordings with `--network none`, every model reported as
      `syn-scripted-agent`, a missing profile refused) on **linux/amd64 and
      linux/arm64** against that digest, then sign, verify and tag.

## Tags

- `<commit sha>` (40 hex): every release build.
- `<manifest version>` (for example `1.7.1`) and `v<repo version>` (for
  example `v0.2.0`): only the first time that tag is published. Version tags
  are first-write-wins; a later build never moves them. If a version already
  exists, the build is still published under its commit sha and the run
  warns to bump the version.
- **No `latest`.** It is never published.

Labels on every release image include `agentic.image.channel=release`,
`org.opencontainers.image.revision=<commit>`, and for toolchain and
scripted-agent `org.opencontainers.image.base.name` / `base.digest` naming the
omni or claude-cli digest it was built on.

## Version manifest: `/opt/agentic/version.json`

Every release image carries `/opt/agentic/version.json` (#93), the file
Syntropic137 records as `ImageManifest` and the E2B template hashes as
`expected_manifest_hash` (`agentic_isolation.providers.manifest_hash`). It
holds exactly these fields, no more (Syntropic137 parses it with
`extra="forbid"`):

```json
{
  "build_commit": "<40-hex source commit>",
  "built_at": "<that commit's committer time, UTC, YYYY-MM-DDTHH:MM:SSZ>",
  "components": {
    "claude_cli": "2.1.293",
    "codex_cli": "0.160.1",
    "playwright": "1.63.0",
    "skills_cli": "1.7.0"
  },
  "manifest_digest": "sha256:<sha256 of the image's manifest.yaml>",
  "provider": "claude-cli",
  "provider_version": "<manifest.yaml version>"
}
```

- **Deterministic per commit.** `built_at` is the commit's time, not the build
  clock; every component is an exact pin; keys are sorted with a two-space
  indent and one trailing newline. Two builds of one commit write the same
  bytes, so the manifest hash is known from the commit alone.
- **Components are verified, not declared.** Each is checked against
  `<binary> --version` in the build before it is written. `omni-agent` adds
  `rtk`, `just` and `apss_session_exporter`; `toolchain` inherits omni's and
  adds `rustup` and `bun`; `scripted-agent` inherits claude-cli's, drops
  `claude_cli` and `codex_cli` (it uninstalls both) and adds `scripted_agent`.
  An inheriting build fails if its base has no manifest.
- **Written from build args.** `scripts/version_manifest.py build-args
  <provider>` prints the `AGENTIC_*` args (provider, version, manifest sha256,
  commit, commit time). `build-provider.py` and the release workflow pass them;
  a build without them fails. A local build of a tree with uncommitted tracked
  changes records `<sha>-dirty`.
- **Inheritance is same-commit only.** An inheriting build also fails if its
  base was built from another commit, so a stale local base cannot stamp old
  CLI versions with this commit.
- **Provenance, not tamper evidence.** The file is `0444` and root-owned, but
  `/opt/agentic` itself is owned by the agent user in every image, so a running
  agent could replace it. Read it at provision, before the agent runs, as
  Syntropic137 does.
- **Checked before signing.** `scripts/check-image-version-manifest.sh IMAGE
  PROVIDER [PLATFORM]` runs in the integration gate for all four images, in
  the toolchain and scripted-agent smokes on both architectures, and on amd64
  for claude-cli and omni-agent after their push.

## Consuming

Never promote a tag. Copy the digest and the identity above into one
reviewable consumer PR (Syntropic137 `PINNED_DIGESTS` plus its cosign identity
regex), then run the rollback proof against the previous digest.

## Local builds

```bash
uv run scripts/build-provider.py omni-agent        # tags omni-agent-workspace:latest locally
uv run scripts/build-provider.py toolchain        # FROM omni-agent-workspace:latest
uv run scripts/build-provider.py toolchain --build-arg OMNI_IMAGE=<ref>   # any other base
implementations/docker/images/toolchain/smoke/run.sh agentic-workspace-toolchain:latest
uv run scripts/build-provider.py claude-cli       # tags agentic-workspace-claude-cli:latest locally
mapfile -t version_args < <(uv run --no-project scripts/version_manifest.py build-args scripted-agent --format docker)
docker build -f implementations/docker/images/scripted-agent/Dockerfile \
  --build-arg CLAUDE_IMAGE=agentic-workspace-claude-cli:latest \
  "${version_args[@]}" \
  -t agentic-workspace-scripted-agent:latest .      # repository-root context, not staged
implementations/docker/images/scripted-agent/smoke/run.sh agentic-workspace-scripted-agent:latest
```

Local tags such as `:latest` exist only in your Docker daemon; they are what
the integration tests resolve by default and are never pushed.
