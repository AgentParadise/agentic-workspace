---
title: "ADR-042: Harness credential presentation for brokered model keys"
status: accepted
created: 2026-10-14
updated: 2026-10-14
author: syntropic137-swe-mini
tags: [credentials, harness, claude, codex, broker]
---

# ADR-042: Harness credential presentation for brokered model keys

## Status

Accepted as the result of spike S1 in the syntropic137 #1735 credential-broker
plan (`docs/plans/20261014_credential-broker.md`, sections D4, D6 and §4 "S1").
PR 6 of that plan turns the presenters here into the shipped implementation.

## Context

Syntropic137 is moving upstream model keys out of the sandbox. The sandbox
holds a per-workspace capability instead: host-generated entropy that a
broker trades for the real key. The plan's hash contract:

1. the host generates the entropy,
2. calls the presenter's `wire_credential(entropy)`,
3. stores `sha256(wire)`,
4. hands `wire` to `present(wire, base_url)`, which returns the env and files
   the sandbox gets.

The broker hashes the value from the standard HTTP auth scheme (`x-api-key`,
the `Bearer` parameter) and does no CLI-specific unwrapping. So S1 had to show,
for each pinned CLI, a presentation that passes the CLI's local checks and puts
exactly `wire` on the wire, at `<base_url>/anthropic/...` or
`<base_url>/openai/...`.

Harness-specific knowledge belongs in this repository (`AGENTS.md`: core code
does not branch on harness brands). Syntropic137 owns the
`HarnessCredentialPresenter` Protocol and never spells a harness env var or
config key.

## Measurements

These were taken on 2026-10-14 against the versions pinned in
`implementations/docker/images/{claude-cli,omni-agent}/Dockerfile`: Claude Code
`2.1.293` and Codex `0.160.1`. Each CLI ran against a loopback fake upstream
with synthetic capabilities only. Every row was run.

| CLI | Presentation | Result |
|---|---|---|
| Claude Code 2.1.293 | `ANTHROPIC_BASE_URL=<base>/anthropic`, `ANTHROPIC_API_KEY=<wire>`, wire = entropy (no `sk-ant-` shape) | **Passes.** One `POST /anthropic/v1/messages?beta=true` with `x-api-key: <wire>`, no `authorization`. Exit 0, nothing written to disk contains the wire. |
| Codex 0.160.1 | `OPENAI_BASE_URL=<base>/openai/v1`, `CODEX_API_KEY=<wire>` | **Fails.** The env var is ignored: Codex dialled `wss://api.openai.com/v1/responses` and then `https://api.openai.com/v1/responses` directly. In a sandbox with deny-all egress this fails closed, but it never reaches the broker. |
| Codex 0.160.1 | config `openai_base_url = "<base>/openai/v1"`, `CODEX_API_KEY=<wire>` | **Routes, but not one request.** `authorization: Bearer <wire>` reaches the broker. Codex first tries WebSocket upgrades (`GET /openai/v1/responses`, `openai-beta: responses_websockets=...`), retries, then falls back to `POST`. |
| Codex 0.160.1 | config provider `model_provider = "broker"`, `base_url = "<base>/openai/v1"`, `wire_api = "responses"`, `requires_openai_auth = true`, `supports_websockets = false`; `CODEX_API_KEY=<wire>` | **Passes.** One `POST /openai/v1/responses`, `authorization: Bearer <wire>`. |
| Codex 0.160.1 | same provider with `env_key = "OPENAI_API_KEY"` instead of `requires_openai_auth`; `OPENAI_API_KEY=<wire>` | **Passes.** One `POST /openai/v1/responses`, `authorization: Bearer <wire>`. Config file carries no secret. |

The "#724 needs a well-formed `sk-ant-...` value" premise in plan D6 does not
hold for the API-key class on the pinned Claude Code: an opaque value is
accepted in `-p` mode.

## Decision

1. **Wire form is the identity for both classes.** `wire_credential(entropy)`
   returns the entropy unchanged. Neither CLI needs a transformed value, so the
   broker's hash is over exactly what the host generated.
2. **Claude Code API key** (`agentic_isolation.harnesses.claude.credentials.ClaudeApiKeyPresenter`):
   env only, `ANTHROPIC_BASE_URL=<base_url>/anthropic` and
   `ANTHROPIC_API_KEY=<wire>`. The capability travels as `x-api-key`.
3. **Codex API-key mode** (`agentic_isolation.harnesses.codex.credentials.CodexApiKeyPresenter`):
   env `OPENAI_API_KEY=<wire>`, plus `$HOME/.codex/config.toml` declaring a
   `syntropic_broker` provider with `base_url = "<base_url>/openai/v1"`,
   `wire_api = "responses"`, `env_key = "OPENAI_API_KEY"` and
   `supports_websockets = false`. The capability travels as
   `authorization: Bearer`.
   `env_key` is chosen over `requires_openai_auth = true` because it reads one
   named variable and nothing else. With `requires_openai_auth`, Codex's login
   machinery is in play, so a ChatGPT `auth.json` in `$CODEX_HOME` could be
   sent instead of the capability.
4. **The presentation type** is `agentic_isolation.harnesses.credentials`:
   frozen `PresentedCredentials(env: Mapping[str, SecretStr],
   files: tuple[PresentedFile, ...])`, where `PresentedFile.path` is relative
   to `$HOME` and cannot escape it. This matches the plan's D6 shape so the
   presenters satisfy the syntropic137 Protocol structurally.
5. Both classes have a passing presentation, so **neither stays
   host-credential-only** on account of S1. Claude OAuth/Max and Codex
   ChatGPT mode were out of scope and stay refused on remote (plan H1, H2).

Python, not Rust (ADR-041): the consumer is syntropic137's Python Protocol
and the existing harness packages are Python. This is an integration that
requires it, not new core.

## Exit test

`lib/python/agentic_isolation/tests/integration/test_harness_credential_presentation.py`,
marked `integration` and skipped without Docker and the image. For each class
it computes the presentation on the host the way production does, runs the
pinned CLI in the `claude-cli` image with `--network none` through
`harness_credential_probe.py` (fake upstream on loopback), and asserts:

- the image's CLI version is the pinned one,
- exactly one request reached the upstream, `POST` at the class's model path,
- the request carries exactly one credential header, the class's standard
  one, once, and its value hashes to `sha256(wire_credential(entropy))`,
- the scan is host-side with full read access (`harness_credential_scan.py`):
  the host samples every process command line through `docker top` while
  the CLI runs, then reads the stopped container's whole filesystem through
  `docker export` and each writable mount through `docker cp`, so a file the
  agent user cannot read (the release image's root-owned 0600 build
  leftovers) is read in full rather than reported unscanned; the CLI's own
  command line must be among the samples and nothing may be left unscanned,
- every credential-named env var in the CLI's environment holds exactly the
  wire, neither the wire nor a host canary is in any file of the sandbox
  filesystem, image files included, and no request, env value, command line
  or file the run wrote holds a credential-shaped string or a canary.

`tests/test_harness_credential_round_trip_checks.py` runs those assertions
without Docker against planted contamination: a repeated auth header, and
sandbox filesystems built as the daemon's tar streams with a canary in a
root-owned 0600 file, a second file, past the first MiB, a writable path
outside `$HOME` and `$TMPDIR`, or a writable mount, the wire in an image
file, a credential-shaped string written outside `$HOME`, a missing CLI
command line, a canary on a command line, and an unscanned location.

Run it with:

```
uv run scripts/build-provider.py claude-cli
cd lib/python/agentic_isolation
uv run --extra dev pytest tests/integration/test_harness_credential_presentation.py -v
```

Mutations checked while writing it (with the pinned CLIs run outside Docker):
setting `supports_websockets = true` fails the Codex class on request count;
adding an `ANTHROPIC_AUTH_TOKEN` to the presentation fails Claude (a second
credential header) and Codex (a stray credential env var).

## Consequences

- The broker routes `/anthropic/*` and `/openai/*`, as plan D4 states. For
  Claude it strips nothing it did not expect: only `x-api-key` arrives. For
  Codex only `authorization` arrives.
- **Codex config ownership.** The Codex presentation writes
  `$HOME/.codex/config.toml`. Workspaces that already write that file (hook
  installation) must merge it rather than overwrite it, and a workspace that
  moves `$CODEX_HOME` must move this file with it. The composer in
  syntropic137 (plan PR 3/5) owns that merge. S1 does not solve it.
- **Pin coupling.** Every result above is a measurement of one CLI version.
  Codex has already changed its base-URL surface (`OPENAI_BASE_URL` is no
  longer honoured). A CLI bump must rerun the exit test. The test asserts the
  version string so it cannot silently pass against a different binary.
- `token_type` is the plain strings `"anthropic"` and `"openai"` for now.
  Aligning it with syntropic137's `TokenType` enum is part of PR 6.
