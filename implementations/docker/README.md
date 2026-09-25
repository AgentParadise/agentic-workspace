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

## Codex sandbox policy

Both providers derive the Codex sandbox policy from the image's
`agentic.codex_cli_version` label at provision (Rust
`DockerProvider::provision`, Python `WorkspaceDockerProvider.create`). Images
with it get `codex-sandbox.json` (Docker's default seccomp profile plus
`clone`/`unshare` for the user, mount, pid, net and ipc namespaces, and
`mount`, `umount2`, `pivot_root`) and, on AppArmor hosts, the AppArmor profile
`agentic-codex-sandbox` (docker-default with `deny mount,` replaced by only the
mounts bubblewrap performs). Images without it keep Docker's defaults, so an
image must carry the label to run Codex sandboxed (an unlabelled one fails
closed at `syn-delegate`'s probe). The container is started by the inspected
image ID, never the tag, so a retag cannot change which image the label
decision applied to.
`with_codex_sandbox(dir)` / `without_codex_sandbox()` assert the expectation;
a contradiction with the label is refused before anything is created.
The AppArmor profile must be loaded on the Docker host with
`sudo apparmor_parser -r`; if it is not, or `docker info` fails, provisioning
fails closed. Capabilities stay dropped and no-new-privileges and the read-only
root stay on. The single sources are
`lib/python/agentic_isolation/agentic_isolation/{seccomp,apparmor}/`, whose
READMEs record provenance, measurement and the trade-off.
