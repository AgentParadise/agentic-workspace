# Docker Implementation

The history-preserved Docker image definitions currently live under
`providers/workspaces/`. The compatibility Python provider lives at
`lib/python/agentic_isolation/agentic_isolation/providers/docker.py`.

The next migration step binds that implementation to the Rust provider port
without changing image behavior or published artifact names.
