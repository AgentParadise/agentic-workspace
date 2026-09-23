# Workspace image release

Tags matching `v*` publish multi-architecture `claude-cli` and `omni-agent`
images. The release workflow runs the entrypoint integration suite first, then
publishes immutable digests with BuildKit SBOM and provenance attestations.

Each digest receives a keyless Sigstore signature. Consumers must verify:

```bash
cosign verify \
  --certificate-identity-regexp \
    '^https://github\.com/AgentParadise/agentic-workspace/\.github/workflows/release-images\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/agentparadise/omni-agent-workspace@sha256:REPLACE_ME
```

Never promote a mutable tag into Syntropic137. Copy the digest and this new
certificate identity into one reviewable consumer PR, then run the rollback
proof against the previous Agentic Primitives digest.
