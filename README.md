<p align="center"><img src="assets/banner.png" alt="Agentic Workspace by Agent Paradise, with the circuit-palm mark" width="960"></p>

# Agentic Workspace

[![CI](https://github.com/AgentParadise/agentic-workspace/actions/workflows/ci.yml/badge.svg)](https://github.com/AgentParadise/agentic-workspace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0D3F49.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-workspace-0D3F49.svg)](#documentation)

A provider-neutral agent workspace contract with Local and Docker
implementations. Future E2B, SBX, and VPS providers plug into the same port.

## Status

- APSS contract: `EXP-V1-0006`, version `0.1.0`, pinned at an immutable APSS commit
- Local: insecure test implementation in Rust
- Docker: Rust isolated adapter plus history-preserved runtime, images, and Python compatibility provider
- Syntropic137 migration: not yet cut over

Local is never a security boundary. It requires explicit opt-in and refuses
production mode. Docker remains the isolated implementation used by Syntropic.

[Architecture](#layout) · [Documentation](#documentation) · [Validation](#validate)

`workspace-core` compiles against the APSS Workspace experiment and delegates
manifest semantic validation to it before applying provider-boundary checks.
`APSS.yaml`, `apss.lock`, and `Cargo.lock` record the project declaration,
standard version, and immutable source commit.

## Documentation

- [APSS declaration](APSS.yaml) and [resolved standard pin](apss.lock)
- [Conformance coverage](tests/conformance/COVERAGE.md) and [known discrepancies](tests/conformance/DISCREPANCIES.md)
- [Docker implementation](implementations/docker) and [Local implementation](implementations/local)

## Layout

```text
crates/workspace-core/       provider-neutral types and port
implementations/local/       insecure filesystem and process adapter
implementations/docker/      Rust Docker adapter, image definitions, and tmux drivers
implementations/docker/images/          Dockerfiles, manifests, fixtures
implementations/docker/interactive-tmux/ host-side Python and Rust drivers
workspace/                   shared image runtime and capabilities
lib/python/                  compatibility packages
plugins/                     frozen Claude image compatibility snapshot
tests/conformance/           requirement coverage and fixtures
```

## Validate

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
REQUIRE_DOCKER_CONFORMANCE=1 cargo test -p agentic-workspace-docker --test conformance
```

Python compatibility packages are tested with `uv run pytest` from each
package directory. Docker build contexts can be staged with:

```bash
uv run scripts/build-provider.py claude-cli --stage-only
```
