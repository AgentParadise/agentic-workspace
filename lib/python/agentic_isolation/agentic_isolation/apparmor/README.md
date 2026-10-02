# AppArmor profiles

## `agentic-codex-sandbox`

The AppArmor half of the Codex sandbox policy. It pairs with
`../seccomp/codex-sandbox.json` and is applied only to images that declare
Codex with the `agentic.codex_cli_version` label (see the seccomp README for
how the providers derive the policy), and only when the Docker daemon reports
AppArmor (`docker info` security options contain `name=apparmor`). Hosts
without AppArmor, such as Docker Desktop, get no AppArmor option at all. A
`docker info` that fails is an error, never "no AppArmor", and is not cached.

### Why it is needed

Measured on a GitHub `ubuntu-latest` runner (Ubuntu 24.04.5, kernel
6.17 azure, AppArmor parser 4.0.1, `kernel.apparmor_restrict_unprivileged_userns=1`)
with codex-cli 0.156.1 and the Codex seccomp profile:

- User-namespace creation already works under `docker-default`; the
  userns restriction is not the blocker (docker-default is an ABI 3.0
  profile, so no `userns,` rule is required, and none is added).
- bubblewrap then fails with `bwrap: Failed to make / slave: Permission denied`
  because `docker-default` contains `deny mount,`.

### Provenance

Rendered from the `docker-default` template in
`vendor/github.com/moby/profiles/apparmor/template.go` of moby/moby tag
`docker-v29.8.0` (commit `3ce5872b7950c63ba2ffbc5123101019ff3e6682`,
module `github.com/moby/profiles/apparmor v0.2.2-0.20260828104831-61eaf32614c7`),
with `DaemonProfile=unconfined`, `abi/3.0`, `tunables/global` and
`abstractions/base`, which is what dockerd renders on Ubuntu. The profile name
is `agentic-codex-sandbox`.

### The one change

`deny mount,` is replaced by exactly the mount operations codex-cli 0.156.1's
bubblewrap performs for `sandbox_mode` read-only and workspace-write, plus
explicit denials. The set was recorded in complain mode on the runner with a
workspace-shaped container (`/workspace` bind mount, `/spool`, `/var/agentic`,
`/tmp` and home tmpfs, read-only root, cwd `/workspace` and
`/workspace/repos/x`). Each rule names its exact option set, source and target:

| Step | Operations |
|---|---|
| staging | `rslave` on `/`; tmpfs on `/tmp/`; bind `/tmp/newroot/` onto itself; `pivot_root` into `/tmp/` |
| sandbox tree | tmpfs on `/newroot/`; bind `/oldroot/` to `/newroot/`, or `/oldroot/usr/`, `usr/bin`, `usr/sbin`, `usr/lib`, `usr/lib64`, `etc` to their `/newroot/` names |
| devices | tmpfs on `/newroot/dev/`; bind `null`, `zero`, `full`, `random`, `urandom`, `tty` each to itself; `devpts` on `/newroot/dev/pts/`; `proc` on `/newroot/proc/` |
| writable roots | bind `/oldroot/tmp/`, the `codex-bwrap-synthetic-mount-targets-*` dir, `/oldroot/workspace/...` and the child journal partition `/oldroot/spool/.agentic-session-store/*/...` to the same place under `/newroot/`; read-only tmpfs masks for `.git`, `.codex`, `.agents` in those roots and the `codex-daemon-*` dir |
| remounts | read-only (three exact flag sets, all with `ro`) anywhere under `/newroot/`, which only narrows; read-write only on `/newroot/workspace/...` and the journal partition |
| switch | `rprivate` on `/oldroot/`; final `pivot_root` into `/newroot/` |
| explicit deny | `sysfs`, `cgroup`, `cgroup2`, `securityfs`, `debugfs`, `tracefs`, `bpf` filesystems; any bind from `/proc`, `/sys`, `/run`, `/var/run` (or their `/oldroot/` views); any bind of a `docker.sock`; a writable remount of `/newroot/{etc,usr,bin,sbin,lib,lib64,proc,sys,dev}` |

Consequence: Codex's working directory must be under `/workspace`
(workspace-write binds it as a writable root). A Codex sandbox started from
elsewhere is refused by the profile, and `syn-delegate`'s live probe reports
that before launching.

**Child journal partition (agentic-workspace#19).** `syn-delegate` gives a
workspace-write Codex delegate one extra writable root, the child journal's
partition directory `/spool/.agentic-session-store/<partition>/`, so the
delegate can itself delegate. The profile admits exactly what bwrap does for
it: a bind of `/oldroot/spool/.agentic-session-store/*/` (and below) to the
same place under `/newroot/`, the `.git`/`.codex`/`.agents` tmpfs masks inside
it, and a read-write remount there. The `*/` needs a partition component, so
the spool root and the metadata namespace itself stay denied. Measured on a
GitHub `ubuntu-latest` runner in enforce mode with the pinned
omni-agent image (codex-cli 0.156.1), 2026-10-02:

| Writable root | Previous profile | This profile |
|---|---|---|
| `/spool/.agentic-session-store/run` | denied (bind) | allowed |
| `/spool` | denied | denied |
| `/spool/.agentic-session-store` | denied | denied |
| none beyond `/workspace` | allowed | allowed |

With the bind rule alone bwrap failed on the `.git` mask, which is why the
mask rule is there. `test_pinned_depth_three.py` (all five cases) passed under
this profile and fails under the previous one (the Codex delegate is refused
with `codex_sandbox_unavailable`).

AppArmor cannot say "source equals target", so the workspace bind rule allows
any `/workspace` subdirectory onto any other; both sides stay inside the
workspace the container can already write.

### Verification (runner, enforce mode)

With a static probe issuing exact `mount(2)`/`pivot_root(2)` calls after
emulating bwrap's staging, every bwrap-shaped operation above succeeded and
every one of these was denied: bind of `/oldroot/proc`, `/oldroot/sys`,
`/oldroot/sys/fs/cgroup`, a `docker.sock` and `/oldroot/spool` into the tree;
bind of `/oldroot/etc` into `/newroot/workspace/`; `sysfs`, `cgroup2` and
`proc` mounts on arbitrary targets; tmpfs on an arbitrary target; a read-write
remount of `/newroot/etc` and `/newroot/proc`; `rprivate` on `/`. Both Codex
sandbox modes work from `/workspace` and a subdirectory (workspace-write
writes inside, is denied outside; read-only denies writes).

### Host setup (once per boot, on the Docker host)

```bash
sudo apparmor_parser -r "$(uv run python -c 'from agentic_isolation import codex_sandbox_apparmor_profile_path as p; print(p())')"
```

or copy the file to `/etc/apparmor.d/agentic-codex-sandbox` so it loads at
boot. The file must be loaded on the host that runs the Docker daemon.

If AppArmor is active and the profile is not loaded, launching fails closed
with `AppArmorProfileNotLoadedError` (Python) or `WorkspaceError::Unsupported`
(Rust) naming this command. When load state cannot be read locally (for
example a remote daemon) the option is still passed and Docker's own
`apparmor failed to apply profile` error is translated to the same error.

### Not changed

No capabilities are added, `apparmor=unconfined` is never used, and host
sysctls such as `kernel.apparmor_restrict_unprivileged_userns` are left alone.
