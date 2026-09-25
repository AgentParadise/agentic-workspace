# Docker Implementation

The Rust adapter in this directory implements the same provider port as Local.
It uses a read-only root, drops capabilities, disables privilege escalation,
sets process and requested CPU/memory limits, and makes network policy an
explicit provider configuration.

Docker image definitions live under `images/`; the interactive tmux host
drivers live under `interactive-tmux/`. The compatibility Python provider lives at
`lib/python/agentic_isolation/agentic_isolation/providers/docker.py`.

Until Syntropic137 cuts over, package names, image names, and build-context
layout remain compatibility contracts.

## Codex sandbox seccomp profile

`DockerProvider::with_codex_sandbox(dir)` (Rust) and
`SecurityConfig.production(codex_sandbox=True)` (Python) opt a workspace into
`codex-sandbox.json`: Docker's default seccomp profile plus `clone` (namespace
flags), `unshare`, `mount`, `umount2` and `pivot_root`, so Codex's bubblewrap
sandbox can create a user namespace. On AppArmor hosts they also apply the
AppArmor profile `agentic-codex-sandbox` (docker-default with `deny mount,`
replaced by the mounts bubblewrap makes), which must be loaded on the Docker
host with `sudo apparmor_parser -r`; if it is not, provisioning fails closed.
Capabilities stay dropped and no-new-privileges and the read-only root stay
on. Use it only for workspaces that can run Codex. The single sources are
`lib/python/agentic_isolation/agentic_isolation/{seccomp,apparmor}/`, whose
READMEs record provenance, verification and the trade-off.
