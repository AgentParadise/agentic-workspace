use agentic_workspace_conformance::{assert_basic_lifecycle, minimal_manifest};
use agentic_workspace_core::{CommandSpec, SecurityProfile, WorkspaceProvider};
use agentic_workspace_docker::{DockerProvider, NetworkPolicy};
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
            "{{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}} {{.HostConfig.ReadonlyRootfs}}",
            &container,
        ])
        .output()
        .unwrap();
    assert!(inspection.status.success());
    let unshare = provider
        .execute(
            &handle,
            &CommandSpec {
                program: "unshare".into(),
                arguments: vec!["-Um".into(), "true".into()],
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

#[test]
fn codex_sandbox_seccomp_allows_user_namespaces_and_keeps_hardening() {
    if !require_or_skip_docker() {
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let profiles = tempfile::tempdir().unwrap();
    let plain = DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None).unwrap();
    let (allowed, inspection) = user_namespace_probe(&plain, "docker-default-seccomp");
    assert!(
        !allowed,
        "Docker's default profile must deny user namespaces"
    );
    assert!(!inspection.contains("seccomp="), "{inspection}");

    let codex = DockerProvider::new(root.path(), "python:3.12-slim", NetworkPolicy::None)
        .unwrap()
        .with_codex_sandbox_seccomp(profiles.path())
        .unwrap();
    let (allowed, inspection) = user_namespace_probe(&codex, "docker-codex-seccomp");
    assert!(
        allowed,
        "the Codex sandbox profile must allow user namespaces"
    );
    assert!(inspection.starts_with("[\"ALL\"] "), "{inspection}");
    assert!(inspection.contains("no-new-privileges"), "{inspection}");
    assert!(inspection.contains("seccomp="), "{inspection}");
    assert!(inspection.ends_with(" true"), "{inspection}");
}
