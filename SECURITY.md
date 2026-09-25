# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report privately through GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/AgentParadise/agentic-workspace/security/advisories/new>).

Include what you found, how to reproduce it, and the affected commit, image
digest or release tag. We aim to acknowledge reports within 5 business days
and will keep you updated while we work on a fix. We credit reporters in the
advisory unless you ask us not to.

## Scope

In scope:

- Isolation or credential-handling flaws in the Docker implementation and the
  workspace runtime (`implementations/docker/`, `workspace/`).
- The published container images and their release, signing and attestation
  pipeline.
- The Rust crates and Python packages in this repository.

Out of scope:

- The Local implementation (`implementations/local/`). It is documented as
  insecure, test-only, and refuses production mode; lack of isolation there is
  expected.
- Vulnerabilities in upstream agent CLIs (Claude Code, Codex) or base images;
  report those to their maintainers. Tell us if a pinned version here is
  affected and we will bump it.

## Supported versions

Only the latest release and `main` receive security fixes.
