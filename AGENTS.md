# Agentic Workspace

Provider-neutral agent workspace contracts and implementations.

## Rules

- APSS EXP-V1-0006 is the normative workspace contract.
- Core code must not branch on Docker, E2B, SBX, VPS, or harness brands.
- Providers implement the shared port through dependency injection.
- Local is insecure, explicit, test-only, and forbidden in production.
- Docker must fail closed on isolation and credential policy.
- Preserve Python package and image compatibility until Syntropic137 cuts over.
- Prefer Rust for new core, provider, and conformance tooling.
- Use `uv` for all Python commands.
- Pin GitHub Actions and release inputs.
- Do not use em dashes.

Run `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
`cargo test --workspace`, and the affected Python package tests before committing.

- Never commit absolute home paths or real infrastructure hostnames. See [docs/PII-HYGIENE.md](docs/PII-HYGIENE.md).
