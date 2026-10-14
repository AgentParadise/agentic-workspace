#[path = "support/process_tree_cancel.rs"]
mod process_tree_cancel;

use agentic_workspace_conformance::{assert_basic_lifecycle, minimal_manifest};
use agentic_workspace_core::{
    CommandSpec, LaunchManifest, SecurityProfile, WorkspaceHandle, WorkspaceProvider,
};
use agentic_workspace_docker::{CODEX_IMAGE_LABEL, DockerProvider, NetworkPolicy};
use std::collections::BTreeMap;
use std::process::Command;
use std::time::Duration;

fn docker_available() -> bool {
    Command::new("docker")
        .args(["info", "--format", "{{.ServerVersion}}"])
        .output()
        .is_ok_and(|output| output.status.success())
}

fn require_or_skip_docker() -> bool {
    if !cfg!(target_os = "linux") {
        eprintln!("Docker isolation conformance runs on Linux only");
        return false;
    }
    if docker_available() {
        true
    } else if std::env::var_os("REQUIRE_DOCKER_CONFORMANCE").is_some() {
        panic!("Docker conformance was required but Docker is unavailable");
    } else {
        eprintln!("Docker unavailable; skipping Docker conformance");
        false
    }
}

/// A provisioned workspace destroyed when dropped, so a failed assertion or
/// unwrap never leaves its container behind. Drop ignores the result: it may
/// run while unwinding, where a second panic aborts, and a test may already
/// have destroyed the workspace (destroy is idempotent).
struct Provisioned<'a> {
    provider: &'a DockerProvider,
    handle: WorkspaceHandle,
}

impl Drop for Provisioned<'_> {
    fn drop(&mut self) {
        let _ = self.provider.destroy(&self.handle);
    }
}

impl std::ops::Deref for Provisioned<'_> {
    type Target = WorkspaceHandle;

    fn deref(&self) -> &WorkspaceHandle {
        &self.handle
    }
}

/// Provision under an execution id unique to this run, so a concurrent or
/// retried run never meets a container left by another.
fn provision<'a>(provider: &'a DockerProvider, manifest: &LaunchManifest) -> Provisioned<'a> {
    let mut manifest = manifest.clone();
    manifest.execution_id = unique_id(&manifest.execution_id);
    let handle = provider.provision(&manifest).unwrap();
    Provisioned { provider, handle }
}

fn unique_id(prefix: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{}-{nanos:x}", std::process::id())
}

fn container_exists(name: &str) -> bool {
    Command::new("docker")
        .args(["container", "inspect", name])
        .output()
        .unwrap()
        .status
        .success()
}

#[test]
fn a_panicking_test_still_removes_its_container() {
    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest("docker-panic-cleanup", SecurityProfile::Isolated);
    let mut container = None;
    let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let handle = provision(&provider, &manifest);
        let name = handle.provider_reference.clone().unwrap();
        assert!(container_exists(&name), "{name} was never created");
        container = Some(name);
        panic!("an assertion failing mid-test");
    }));
    assert!(unwound.is_err());
    let container = container.unwrap();
    assert!(
        !container_exists(&container),
        "{container} survived the panic"
    );
}

#[test]
fn passes_shared_functional_conformance() {
    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest(unique_id("docker-conformance"), SecurityProfile::Isolated);
    assert_basic_lifecycle(
        &provider,
        &manifest,
        CommandSpec {
            program: "sh".into(),
            arguments: vec!["-c".into(), "printf 'conformant\\n' > artifact.txt".into()],
            environment: BTreeMap::new(),
        },
        "work/artifact.txt",
    )
    .unwrap();
}

/// The workspace images Syntropic137 pins, by digest. This repo publishes the
/// images but not the pins: they are copied from syntropic137
/// `packages/syn-shared/src/syn_shared/settings/workspace_images.py`
/// (`PINNED_DIGESTS`, with the repository names from `workspace_image_name`).
/// The claude-cli image is published as `agentic-workspace-claude`.
const PINNED_WORKSPACE_IMAGES: [&str; 3] = [
    "ghcr.io/agentparadise/agentic-workspace-claude@sha256:927641f9e7f1d18a19fce9c11f9923ef68e2e498ca48105cc32de21122614843",
    "ghcr.io/agentparadise/agentic-workspace-omni-agent@sha256:2cabc694d450807ca953ea634ca0d50d53fcfdb6864be58c91464ab5008fe6e4",
    "ghcr.io/agentparadise/agentic-workspace-toolchain@sha256:4a76c184a8d36b8663a0f0191842410304ea1e10b0e719516817949ae8c862f8",
];

/// Feasibility gate for cancelling an agent's whole process tree inside each
/// pinned image: `setsid` and `kill` are on PATH, and the trusted-pid wrapper
/// plus group TERM -> KILL leaves no recorded pid alive. A missing binary
/// fails here with `MISSING <binary>` in the message.
#[test]
fn pinned_images_can_cancel_the_whole_process_tree() {
    if !require_or_skip_docker() {
        return;
    }
    for image in PINNED_WORKSPACE_IMAGES {
        let output = Command::new("docker")
            .args(["run", "--rm", "--network", "none", "--entrypoint", "sh"])
            .args([
                image,
                "-c",
                process_tree_cancel::SCRIPT,
                "process_tree_cancel",
            ])
            .output()
            .unwrap();
        process_tree_cancel::assert_cancels_whole_tree(&output, image);
    }
}

#[test]
fn enforces_isolation_and_kills_timed_out_container() {
    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let mut manifest = minimal_manifest("docker-security", SecurityProfile::Isolated);
    manifest.limits.memory_mb = Some(128);
    manifest.limits.cpu_millis = Some(500);
    let handle = provision(&provider, &manifest);
    let container = handle.provider_reference.as_deref().unwrap();

    let inspection = Command::new("docker")
        .args([
            "inspect",
            "--format",
            "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.NetworkMode}}",
            container,
        ])
        .output()
        .unwrap();
    assert!(inspection.status.success());
    assert_eq!(
        String::from_utf8_lossy(&inspection.stdout).trim(),
        "134217728 500000000 none"
    );

    let readonly = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "sh".into(),
                arguments: vec!["-c".into(), "touch /etc/should-fail".into()],
                environment: BTreeMap::new(),
            },
            Duration::from_secs(5),
        )
        .unwrap();
    assert_ne!(readonly.exit_code, Some(0));

    let capabilities = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "sh".into(),
                arguments: vec!["-c".into(), "grep '^CapEff:' /proc/self/status".into()],
                environment: BTreeMap::new(),
            },
            Duration::from_secs(5),
        )
        .unwrap();
    assert!(String::from_utf8_lossy(&capabilities.stdout).contains("0000000000000000"));

    let timeout = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "sleep".into(),
                arguments: vec!["30".into()],
                environment: BTreeMap::new(),
            },
            Duration::from_millis(100),
        )
        .unwrap();
    assert!(timeout.timed_out);
    provider.destroy(&handle).unwrap();
    provider.destroy(&handle).unwrap();
}

fn user_namespace_probe(provider: &DockerProvider, id: &str) -> (bool, String) {
    let manifest = minimal_manifest(id, SecurityProfile::Isolated);
    let handle = provision(provider, &manifest);
    let container = handle.provider_reference.clone().unwrap();
    let inspection = Command::new("docker")
        .args([
            "inspect",
            "--format",
            "{{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}} {{.HostConfig.ReadonlyRootfs}} apparmor={{.AppArmorProfile}}",
            &container,
        ])
        .output()
        .unwrap();
    assert!(inspection.status.success());
    // What bubblewrap does first: a user and mount namespace, then its
    // staging tmpfs over /tmp (flags exactly as bwrap passes them). The
    // seccomp profile gates the first, AppArmor (on AppArmor hosts) the second.
    let unshare = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "unshare".into(),
                // mount(2) itself: util-linux mount(8) uses the new fsopen()
                // API, which bwrap does not use and the profile does not open.
                arguments: vec![
                    "-U".into(),
                    "--map-root-user".into(),
                    "-m".into(),
                    "--propagation".into(),
                    "unchanged".into(),
                    "python3".into(),
                    "-c".into(),
                    "import ctypes, sys\n\
                     libc = ctypes.CDLL(None, use_errno=True)\n\
                     sys.exit(libc.mount(b'tmpfs', b'/tmp', b'tmpfs', 6, None) != 0)"
                        .into(),
                ],
                environment: BTreeMap::new(),
            },
            Duration::from_secs(10),
        )
        .unwrap();
    provider.destroy(&handle).unwrap();
    (
        unshare.exit_code == Some(0),
        String::from_utf8_lossy(&inspection.stdout)
            .trim()
            .to_owned(),
    )
}

const BASE_IMAGE: &str = "python:3.12-slim";
const PLAIN_IMAGE: &str = "agentic-workspace-conformance-plain:local";
const CODEX_IMAGE: &str = "agentic-workspace-conformance-codex:local";

/// Two images that differ only in the capability label. Both run as a
/// non-root user, as workspace agents do: mapping uid 0 into a user namespace
/// needs CAP_SETFCAP, which a capability-free container does not have.
fn build_images() {
    build_image(PLAIN_IMAGE, "");
    build_image(
        CODEX_IMAGE,
        &format!("LABEL {CODEX_IMAGE_LABEL}=conformance\n"),
    );
}

fn build_image(tag: &str, extra: &str) {
    let mut build = Command::new("docker")
        .args(["build", "--quiet", "--tag", tag, "-"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    use std::io::Write;
    build
        .stdin
        .take()
        .unwrap()
        .write_all(
            format!(
                "FROM {BASE_IMAGE}\n{extra}\
                 RUN mkdir -p /spool/.agentic-session-store/run /spool/run/claude /spool/run/codex \
                 /spool/exec/ws/claude /spool/.hidden/claude /spool/.agentic-session-store/exec/ws \
                 && chown -R 1000:1000 /spool\n\
                 USER 1000:1000\n"
            )
            .as_bytes(),
        )
        .unwrap();
    assert!(build.wait().unwrap().success());
}

fn docker_apparmor_active() -> bool {
    let info = Command::new("docker")
        .args(["info", "--format", "{{json .SecurityOptions}}"])
        .output()
        .unwrap();
    assert!(info.status.success());
    String::from_utf8_lossy(&info.stdout).contains("name=apparmor")
}

#[test]
fn codex_sandbox_policy_follows_the_image_and_keeps_hardening() {
    if !require_or_skip_docker() {
        return;
    }
    build_images();
    let root = tempfile::tempdir().unwrap();

    let plain = DockerProvider::new(root.path(), PLAIN_IMAGE, NetworkPolicy::None).unwrap();
    let (allowed, inspection) = user_namespace_probe(&plain, "docker-default-seccomp");
    assert!(!allowed, "Docker's defaults must deny the bwrap staging");
    assert!(!inspection.contains("seccomp="), "{inspection}");

    let codex = DockerProvider::new(root.path(), CODEX_IMAGE, NetworkPolicy::None).unwrap();
    let (allowed, inspection) = user_namespace_probe(&codex, "docker-codex-derived");
    assert!(
        allowed,
        "the Codex sandbox policy must allow the bwrap staging"
    );
    assert!(inspection.starts_with("[\"ALL\"] "), "{inspection}");
    assert!(inspection.contains("no-new-privileges"), "{inspection}");
    assert!(inspection.contains("seccomp="), "{inspection}");
    assert!(inspection.contains(" true apparmor="), "{inspection}");
    if docker_apparmor_active() {
        assert!(
            inspection.ends_with("apparmor=agentic-codex-sandbox"),
            "{inspection}"
        );
    } else {
        assert!(!inspection.contains("apparmor=agentic"), "{inspection}");
    }

    for provider in [
        DockerProvider::new(root.path(), PLAIN_IMAGE, NetworkPolicy::None)
            .unwrap()
            .with_codex_sandbox(root.path().join("profiles")),
        DockerProvider::new(root.path(), CODEX_IMAGE, NetworkPolicy::None)
            .unwrap()
            .without_codex_sandbox(),
    ] {
        let manifest = minimal_manifest("docker-codex-mismatch", SecurityProfile::Isolated);
        let error = provider.provision(&manifest).unwrap_err();
        assert!(error.to_string().contains(CODEX_IMAGE_LABEL), "{error}");
        assert!(!root.path().join("docker-codex-mismatch").exists());
    }
}

const MOUNT_POLICY_PROBE: &str = include_str!("fixtures/mount_policy_probe.py");

/// On an AppArmor host, the Codex policy admits bwrap's exact mounts and
/// denies sensitive ones. Without AppArmor only the kernel mediates mounts
/// inside the container's own user namespace, so only the allowed half and
/// the kernel-locked denials are asserted there.
#[test]
fn codex_mount_policy_admits_bwrap_and_denies_sensitive_mounts() {
    if !require_or_skip_docker() {
        return;
    }
    build_images();
    let root = tempfile::tempdir().unwrap();
    let provider = DockerProvider::new(root.path(), CODEX_IMAGE, NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest("docker-mount-policy", SecurityProfile::Isolated);
    let handle = provision(&provider, &manifest);
    let result = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "python3".into(),
                arguments: vec!["-c".into(), MOUNT_POLICY_PROBE.into()],
                environment: BTreeMap::new(),
            },
            Duration::from_secs(30),
        )
        .unwrap();
    provider.destroy(&handle).unwrap();
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert_eq!(
        result.exit_code,
        Some(0),
        "{stdout} {}",
        String::from_utf8_lossy(&result.stderr)
    );
    let apparmor = docker_apparmor_active();
    let mut checked = 0;
    for entry in stdout
        .trim()
        .trim_matches(|c| c == '{' || c == '}')
        .split("], ")
    {
        let (label, outcome) = entry.split_once(": [").unwrap();
        let label = label.trim_matches('"');
        let (allowed, errno) = outcome.trim_end_matches(']').split_once(", ").unwrap();
        let allowed = allowed == "true";
        if label.starts_with("allowed: rw remount ")
            && label != "allowed: rw remount workspace"
            && !allowed
        {
            // This container's root is read-only, so the kernel refuses a
            // writable remount of anything bound from it (EPERM). AppArmor is
            // consulted first and would refuse with EACCES, which this rules
            // out. In a workspace the spool is a writable volume.
            assert_eq!(errno, "1", "{label} must be allowed by policy: {stdout}");
        } else if label.starts_with("staging") || label.starts_with("allowed:") {
            assert!(allowed, "{label} must be allowed: {stdout}");
        } else if apparmor || label.contains("rw remount") {
            assert!(!allowed, "{label} must be denied: {stdout}");
            // EACCES (AppArmor) or EPERM (kernel lock), never a missing path.
            assert!(errno == "13" || errno == "1", "{label} errno {errno}");
        }
        checked += 1;
    }
    assert_eq!(checked, 41, "{stdout}");
}

/// The Rust `SupportsOperations` against a real container: cancel ends the
/// leader, a grandchild, a child that left the group and a TERM-trapper,
/// confirmed through the container's own `/proc`, without a container stop.
#[test]
fn operations_cancel_the_whole_tree_in_a_real_container() {
    use agentic_workspace_core::operations::{
        ObserveRequest, OperationSpec, OperationState, SupportsOperations, WorkspaceRef,
    };
    use agentic_workspace_docker::operations::DockerOperations;
    use base64::Engine as _;

    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest("docker-operations", SecurityProfile::Isolated);
    let handle = provision(&provider, &manifest);
    let container = handle.provider_reference.clone().unwrap();
    let workspace: WorkspaceRef =
        serde_json::from_value(serde_json::json!({"provider": "docker", "resource_id": container}))
            .unwrap();
    let spec: OperationSpec = serde_json::from_value(serde_json::json!({
        "execution_id": "exec-docker",
        "operation_key": "k1",
        "argv": ["sh", "-c", "trap '' TERM; \
            (trap '' TERM; while :; do sleep 1; done) & echo trapper $!; \
            (sleep 300 & echo grandchild $!; wait) & \
            setsid sleep 300 & echo escapee $!; \
            echo leader $$; wait"],
        "working_directory": manifest.workspace.working_directory,
        "environment": {},
        "credential_bindings": [],
        "timeout_seconds": 120,
        "metadata": {},
    }))
    .unwrap();
    let ops = DockerOperations::new(
        "docker",
        std::num::NonZeroU64::new(1 << 20).unwrap(),
        std::num::NonZeroU64::new(1 << 16).unwrap(),
    );
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let (status, pids) = runtime.block_on(async {
        let op = ops.start(&workspace, spec).await.unwrap();
        let (mut pids, mut cursor) = (Vec::<String>::new(), None);
        while pids.len() < 4 {
            let request = ObserveRequest {
                cursor,
                max_bytes: 4096,
                wait: Duration::from_secs(10),
            };
            let page = ops.observe(&op, request).await.unwrap();
            let data = base64::engine::general_purpose::STANDARD
                .decode(page.data_b64.as_str())
                .unwrap();
            let text = String::from_utf8(data).unwrap();
            pids.extend(
                text.split_whitespace()
                    .filter(|w| w.parse::<u32>().is_ok())
                    .map(String::from),
            );
            cursor = Some(page.next_cursor.to_string());
            assert!(!page.complete, "ended before cancel: {text}");
        }
        (ops.cancel(&op, Duration::from_secs(1)).await.unwrap(), pids)
    });
    assert_eq!(status.state, OperationState::Cancelled);
    assert!(
        !status.metadata.get().contains_key("escalation"),
        "{status:?}"
    );
    for pid in &pids {
        let stat = Command::new("docker")
            .args(["exec", &container, "cat", &format!("/proc/{pid}/stat")])
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&stat.stdout);
        let live = stat.status.success() && !text.rsplit_once(") ").unwrap().1.starts_with('Z');
        assert!(!live, "pid {pid} survived cancel");
    }
    provider.destroy(&handle).unwrap();
}

/// The Rust `SupportsOperations` against a real container: a double-forked
/// descendant that the container's init adopted before cancel first looked
/// has no parent chain back to the operation, so cancel must fail closed into
/// a container stop, confirmed stopped, and nothing of the operation survives.
#[test]
fn operations_cancel_stops_the_container_for_a_double_forked_orphan() {
    use agentic_workspace_core::operations::{
        ObserveRequest, OperationSpec, OperationState, SupportsOperations, WorkspaceRef,
    };
    use agentic_workspace_docker::operations::DockerOperations;
    use base64::Engine as _;

    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest("docker-operations-orphan", SecurityProfile::Isolated);
    let handle = provision(&provider, &manifest);
    let container = handle.provider_reference.clone().unwrap();
    let workspace: WorkspaceRef =
        serde_json::from_value(serde_json::json!({"provider": "docker", "resource_id": container}))
            .unwrap();
    let spec: OperationSpec = serde_json::from_value(serde_json::json!({
        "execution_id": "exec-docker",
        "operation_key": "k1",
        "argv": ["sh", "-c",
            "setsid sh -c 'sleep 300 & echo orphan $!' & wait; echo leader $$; sleep 300"],
        "working_directory": manifest.workspace.working_directory,
        "environment": {},
        "credential_bindings": [],
        "timeout_seconds": 120,
        "metadata": {},
    }))
    .unwrap();
    let ops = DockerOperations::new(
        "docker",
        std::num::NonZeroU64::new(1 << 20).unwrap(),
        std::num::NonZeroU64::new(1 << 16).unwrap(),
    );
    let stat = |pid: &str| {
        let out = Command::new("docker")
            .args(["exec", &container, "cat", &format!("/proc/{pid}/stat")])
            .output()
            .unwrap();
        let text = String::from_utf8_lossy(&out.stdout).into_owned();
        out.status.success().then_some(text)
    };
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let (status, pids) = runtime.block_on(async {
        let op = ops.start(&workspace, spec).await.unwrap();
        let (mut pids, mut cursor) = (Vec::<String>::new(), None);
        while pids.len() < 2 {
            let request = ObserveRequest {
                cursor,
                max_bytes: 4096,
                wait: Duration::from_secs(10),
            };
            let page = ops.observe(&op, request).await.unwrap();
            let data = base64::engine::general_purpose::STANDARD
                .decode(page.data_b64.as_str())
                .unwrap();
            let text = String::from_utf8(data).unwrap();
            pids.extend(
                text.split_whitespace()
                    .filter(|w| w.parse::<u32>().is_ok())
                    .map(String::from),
            );
            cursor = Some(page.next_cursor.to_string());
            assert!(!page.complete, "ended before cancel: {text}");
        }
        // Its parent has exited and the container's init adopted it.
        let adopted = || {
            stat(&pids[0]).is_some_and(|s| {
                s.rsplit_once(") ").unwrap().1.split_whitespace().nth(1) == Some("1")
            })
        };
        for _ in 0..50 {
            if adopted() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert!(adopted(), "orphan {} was never adopted by init", pids[0]);
        (ops.cancel(&op, Duration::from_secs(1)).await.unwrap(), pids)
    });
    assert_eq!(status.state, OperationState::Cancelled);
    assert_eq!(
        status.metadata.get().get("escalation").map(String::as_str),
        Some("container_stopped"),
        "{status:?}"
    );
    let state = Command::new("docker")
        .args([
            "container",
            "inspect",
            "-f",
            "{{.State.Status}}",
            &container,
        ])
        .output()
        .unwrap();
    let state = String::from_utf8_lossy(&state.stdout);
    assert!(state.trim() != "running", "container still {state}");
    // A stopped container runs nothing, so no identity can have survived.
    for pid in &pids {
        assert!(stat(pid).is_none(), "pid {pid} survived cancel");
    }
    provider.destroy(&handle).unwrap();
}
