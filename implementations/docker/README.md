# Docker Implementation

The Rust adapter in this directory implements the same provider port as Local.
It uses a read-only root, drops capabilities, disables privilege escalation,
sets process and requested CPU/memory limits, and makes network policy an
explicit provider configuration.

The history-preserved Docker image definitions remain under
`providers/workspaces/`. The compatibility Python provider lives at
`lib/python/agentic_isolation/agentic_isolation/providers/docker.py`.

Until Syntropic137 cuts over, package names, image names, and build-context
layout remain compatibility contracts.
