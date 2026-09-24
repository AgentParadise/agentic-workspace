---
title: "ADR-041: Rust-first workspace core"
status: accepted
created: 2026-09-23
updated: 2026-09-23
author: NeuralEmpowerment
tags: [rust, python, workspace, contracts]
---

# ADR-041: Rust-first workspace core

## Status

Accepted. This records the direction already stated in `AGENTS.md` and carries
forward the useful decision from [Agentic Primitives PR #244](https://github.com/AgentParadise/agentic-primitives/pull/244).

## Context

Agentic Workspace owns provider-neutral contracts, provider implementations,
runtime libraries, and images. Its execution and contract work spans Rust and
Python. New core logic needs one clear home while existing Python packages and
image interfaces remain compatible through the Syntropic137 cutover.

The earlier proposal named `itmux` as the substrate. That name and its tmux
execution assumptions are not part of this decision. The applicable contract
is APSS EXP-V1-0006, and providers implement shared ports without putting
Docker or a harness brand into the core.

## Decision

New workspace core, provider, contract, and conformance tooling is Rust-first.
Expose cross-language behavior through documented, versioned contracts. Keep
Python for compatibility packages, thin consumers, and integrations that
require it. A new Python implementation of core behavior needs a concrete
reason recorded with that change.

Rust-first is a preference, not a mandate to rewrite working Python code.
Existing Python package and image compatibility remains required until the
consumer cutover is proven. The provider-neutral APSS contract and the
repository's isolation and credential rules remain binding regardless of
implementation language.

## Consequences

- New core behavior has one preferred implementation language and can be
  consumed without requiring Python in every workspace runtime.
- Python consumers may retain a small adapter over the published contract.
- Rust and Python surfaces that coexist during migration need explicit parity
  checks until the compatibility surface can be retired.
- This ADR does not choose tmux, interactive input, headless commands, or a
  completion signal. Those are separate execution-model decisions.
