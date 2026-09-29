use agentic_workspace_conformance::{assert_basic_lifecycle, minimal_manifest};
use agentic_workspace_core::{CommandSpec, SecurityProfile, WorkspaceProvider};
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

#[test]
fn passes_shared_functional_conformance() {
    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let provider =
        DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let manifest = minimal_manifest("docker-conformance", SecurityProfile::Isolated);
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
    let handle = provider.provision(&manifest).unwrap();
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
    let handle = provider.provision(&manifest).unwrap();
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
        .write_all(format!("FROM {BASE_IMAGE}\n{extra}USER 1000:1000\n").as_bytes())
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
    let handle = provider
        .provision(&minimal_manifest(
            "docker-mount-policy",
            SecurityProfile::Isolated,
        ))
        .unwrap();
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
        if label.starts_with("staging") || label.starts_with("allowed:") {
            assert!(allowed, "{label} must be allowed: {stdout}");
        } else if apparmor || label.contains("rw remount") {
            assert!(!allowed, "{label} must be denied: {stdout}");
            // EACCES (AppArmor) or EPERM (kernel lock), never a missing path.
            assert!(errno == "13" || errno == "1", "{label} errno {errno}");
        }
        checked += 1;
    }
    assert_eq!(checked, 21, "{stdout}");
}
