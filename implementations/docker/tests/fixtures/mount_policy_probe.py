"""Exercise the Codex sandbox mount policy with exact mount(2) calls.

Emulates bubblewrap's staging (user + mount namespace, tmpfs over /tmp,
pivot_root), then tries bwrap-shaped operations and sensitive ones. Prints one
JSON object: {"<label>": [allowed, errno]}. Every source exists, so a denial
is the policy (EACCES/EPERM), never a missing path. Run as a non-root user
inside a capability-free container whose /workspace is writable.
"""

import ctypes
import json
import os
import platform
import socket

MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_REC, MS_SILENT = 32, 4096, 16384, 32768
MS_PRIVATE, MS_SLAVE, MS_RELATIME = 1 << 18, 1 << 19, 1 << 21
PIVOT_ROOT = {"x86_64": 155, "aarch64": 41}[platform.machine()]

libc = ctypes.CDLL(None, use_errno=True)


def mount(source: str | None, target: str, fstype: str | None, flags: int) -> list[object]:
    def encode(value: str | None) -> bytes | None:
        return None if value is None else value.encode()

    ok = libc.mount(encode(source), encode(target), encode(fstype), flags, None) == 0
    return [ok, 0 if ok else ctypes.get_errno()]


# A real unix socket named like Docker's, before the namespace switch.
sock = socket.socket(socket.AF_UNIX)
sock.bind("/workspace/docker.sock")


uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
with open("/proc/self/setgroups", "w") as handle:
    handle.write("deny")
with open("/proc/self/uid_map", "w") as handle:
    handle.write(f"0 {uid} 1")
with open("/proc/self/gid_map", "w") as handle:
    handle.write(f"0 {gid} 1")

results: dict[str, list[object]] = {}
results["staging tmpfs /tmp"] = mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV)
for path in ("/tmp/newroot", "/tmp/oldroot"):
    os.makedirs(path, exist_ok=True)
results["staging bind newroot"] = mount("/tmp/newroot", "/tmp/newroot", None, MS_BIND | MS_REC)
os.chdir("/tmp")
pivoted = libc.syscall(PIVOT_ROOT, b".", b"oldroot") == 0
results["staging pivot_root"] = [pivoted, 0 if pivoted else ctypes.get_errno()]
for path in ("p", "s", "c", "e", "etc", "workspace/etc", "workspace/w", "dev"):
    os.makedirs(f"/newroot/{path}", exist_ok=True)
open("/newroot/workspace/sock", "w").close()

bind = MS_BIND | MS_REC
remount = MS_REMOUNT | MS_BIND | MS_NOSUID | MS_NODEV | MS_SILENT | MS_RELATIME
checks = {
    # bwrap-shaped: expected allowed
    "allowed: bind etc": ("/oldroot/etc/", "/newroot/etc/", None, bind),
    "allowed: ro remount etc": (None, "/newroot/etc/", None, remount | MS_RDONLY),
    "allowed: bind workspace": ("/oldroot/workspace/", "/newroot/workspace/w/", None, bind),
    "allowed: rw remount workspace": (None, "/newroot/workspace/w/", None, remount),
    "allowed: tmpfs dev": ("tmpfs", "/newroot/dev/", "tmpfs", MS_NOSUID | MS_NODEV),
    # sensitive: expected denied
    "denied: bind proc": ("/oldroot/proc/", "/newroot/p/", None, bind),
    "denied: bind sys": ("/oldroot/sys/", "/newroot/s/", None, bind),
    "denied: bind cgroup": ("/oldroot/sys/fs/cgroup/", "/newroot/c/", None, bind),
    "denied: sysfs": ("sysfs", "/newroot/s/", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC),
    "denied: cgroup2": ("none", "/newroot/c/", "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC),
    "denied: proc elsewhere": ("proc", "/newroot/p/", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC),
    "denied: bind etc into workspace": ("/oldroot/etc/", "/newroot/workspace/etc/", None, bind),
    "denied: bind a docker.sock": (
        "/oldroot/workspace/docker.sock",
        "/newroot/workspace/sock",
        None,
        bind,
    ),
    "denied: bind dev": ("/oldroot/dev/", "/newroot/e/", None, bind),
    "denied: tmpfs elsewhere": ("tmpfs", "/newroot/e/", "tmpfs", MS_NOSUID | MS_NODEV),
    "denied: rw remount etc": (None, "/newroot/etc/", None, remount),
    "denied: rw remount proc": (None, "/newroot/p/", None, remount),
    "denied: rprivate root": (None, "/", None, MS_SILENT | MS_PRIVATE | MS_REC),
}
for label, (source, target, fstype, flags) in checks.items():
    results[label] = mount(source, target, fstype, flags)
print(json.dumps(results))
