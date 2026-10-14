//! The APSS 0.2.0 behaviour fixtures, as named provider tests, against
//! `DockerOperations` driven through a stand-in `docker` whose `exec` runs
//! the command here. That exercises the real wrapper, `setsid`, `/proc` and
//! signal scripts; what it cannot prove (namespaces, `docker stop`) is in the
//! docker-backed `conformance` test.
//!
//! The stand-in needs `setsid` and `/proc`, so the suite is Linux only; the
//! Linux `rust-test` job runs it.
#![cfg(target_os = "linux")]

use agentic_workspace_core::operations::{
    ErrorKind, ObserveRequest, OperationRef, OperationSpec, OperationState, SupportsOperations,
    WorkspaceRef,
};
use agentic_workspace_docker::operations::DockerOperations;
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

/// `exec` drops docker's own flags and runs the command here; `stop` is
/// recorded. Every argv is appended to `docker.log` beside the script.
fn fake_docker(dir: &Path) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    let script = dir.join("docker");
    let log = dir.join("docker.log");
    std::fs::write(
        &script,
        format!(
            r#"#!/bin/sh
printf '%s\n' "$*" >> '{log}'
case "$1" in
exec)
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
        -i) shift ;;
        -w) shift 2 ;;
        -e) export "$2"; shift 2 ;;
        *) break ;;
        esac
    done
    shift
    exec "$@" ;;
stop) exit 0 ;;
esac
"#,
            log = log.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    script
}

fn ops(dir: &Path, retention: u64, page: u64) -> DockerOperations {
    DockerOperations::new(
        fake_docker(dir),
        NonZeroU64::new(retention).unwrap(),
        NonZeroU64::new(page).unwrap(),
    )
}

fn workspace() -> WorkspaceRef {
    serde_json::from_value(serde_json::json!({"provider": "docker", "resource_id": "c1"})).unwrap()
}

fn spec(key: &str, script: &str, timeout: u64) -> OperationSpec {
    serde_json::from_value(serde_json::json!({
        "execution_id": "exec-1",
        "operation_key": key,
        "argv": ["sh", "-c", script],
        "working_directory": "repo",
        "environment": {},
        "credential_bindings": [],
        "timeout_seconds": timeout,
        "metadata": {},
    }))
    .unwrap()
}

/// Every page until `complete`, checking each against the bound.
async fn drain(ops: &DockerOperations, op: &OperationRef, max_bytes: usize) -> Vec<u8> {
    let (mut cursor, mut data) = (None, Vec::new());
    for _ in 0..10_000 {
        let page = ops
            .observe(
                op,
                ObserveRequest {
                    cursor: cursor.clone(),
                    max_bytes,
                    wait: Duration::from_secs(5),
                },
            )
            .await
            .unwrap();
        assert!(serde_json::to_vec(&page).unwrap().len() <= max_bytes);
        data.extend(STANDARD.decode(page.data_b64.as_str()).unwrap());
        if page.complete {
            return data;
        }
        cursor = Some(page.next_cursor.to_string());
    }
    panic!("never complete");
}

fn alive(pid: &str) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .is_ok_and(|stat| !stat.rsplit_once(") ").unwrap().1.starts_with('Z'))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn start_same_key_one_launch_under_50_concurrent_tasks() {
    let dir = tempfile::tempdir().unwrap();
    let ops = Arc::new(ops(dir.path(), 1 << 20, 1 << 16));
    let launches = dir.path().join("launches");
    let script = format!("echo x >> '{}'", launches.display());
    let tasks: Vec<_> = (0..50)
        .map(|_| {
            let (ops, spec) = (ops.clone(), spec("k1", &script, 60));
            tokio::spawn(async move { ops.start(&workspace(), spec).await.unwrap() })
        })
        .collect();
    let mut refs = Vec::new();
    for task in tasks {
        refs.push(task.await.unwrap());
    }
    assert!(refs.iter().all(|r| *r == refs[0]));
    drain(&ops, &refs[0], 4096).await;
    assert_eq!(std::fs::read_to_string(&launches).unwrap(), "x\n");
    let status = ops.status(&refs[0]).await.unwrap();
    assert_eq!(status.state, OperationState::Succeeded);
    assert_eq!(status.exit_code, Some(Some(0)));
}

#[tokio::test]
async fn same_key_different_spec_is_conflict_and_launches_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let first = ops
        .start(&workspace(), spec("k1", "sleep 30", 60))
        .await
        .unwrap();
    let error = ops
        .start(&workspace(), spec("k1", "sleep 31", 60))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::Conflict);
    assert_eq!(error.operation.as_deref(), Some(&first));
    ops.cancel(&first, Duration::from_millis(200))
        .await
        .unwrap();
    let log = std::fs::read_to_string(dir.path().join("docker.log")).unwrap();
    assert_eq!(
        log.lines().filter(|l| l.contains("aw-operation")).count(),
        1
    );
}

#[tokio::test]
async fn cursor_after_retention_reports_the_exact_gap_then_drains() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 64, 4096);
    let op = ops
        .start(
            &workspace(),
            spec("k1", "printf '%0200d' 0; printf tail", 60),
        )
        .await
        .unwrap();
    while !ops.status(&op).await.unwrap().state.is_terminal() {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let page = ops
        .observe(
            &op,
            ObserveRequest {
                cursor: None,
                max_bytes: 4096,
                wait: Duration::ZERO,
            },
        )
        .await
        .unwrap();
    assert_eq!(page.gap.unwrap().lost_bytes, NonZeroU64::new(204 - 64));
    let data = STANDARD.decode(page.data_b64.as_str()).unwrap();
    assert_eq!(data.len(), 64);
    assert!(data.ends_with(b"tail") && page.complete);
}

#[tokio::test]
async fn late_read_drains_bounded_pages_then_completes() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let op = ops
        .start(&workspace(), spec("k1", "seq 1 3000; echo done >&2", 60))
        .await
        .unwrap();
    let data = drain(&ops, &op, 300).await;
    let mut expected: Vec<String> = (1..=3000).map(|n| n.to_string()).collect();
    expected.push("done".into());
    let expected = expected.join("\n") + "\n";
    assert_eq!(String::from_utf8(data).unwrap(), expected);
}

#[tokio::test]
async fn cancel_kills_leader_grandchild_and_term_trapper_and_is_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    // A TERM-trapping child, a grandchild, a child that left the group, and
    // the leader, which traps TERM too.
    let script = "trap '' TERM; \
        (trap '' TERM; while :; do sleep 1; done) & echo trapper $!; \
        (sleep 300 & echo grandchild $!; wait) & \
        setsid sleep 300 & echo escapee $!; \
        echo leader $$; wait";
    let op = ops
        .start(&workspace(), spec("k1", script, 60))
        .await
        .unwrap();
    let mut pids = Vec::new();
    let mut cursor = None;
    while pids.len() < 4 {
        let page = ops
            .observe(
                &op,
                ObserveRequest {
                    cursor,
                    max_bytes: 4096,
                    wait: Duration::from_secs(5),
                },
            )
            .await
            .unwrap();
        let text = String::from_utf8(STANDARD.decode(page.data_b64.as_str()).unwrap()).unwrap();
        pids.extend(
            text.split_whitespace()
                .filter(|w| w.parse::<u32>().is_ok())
                .map(String::from),
        );
        cursor = Some(page.next_cursor.to_string());
    }
    assert!(pids.iter().all(|p| alive(p)), "{pids:?}");
    let first = ops.cancel(&op, Duration::from_millis(300)).await.unwrap();
    assert_eq!(first.state, OperationState::Cancelled);
    assert!(
        !first.metadata.get().contains_key("escalation"),
        "{first:?}"
    );
    for pid in &pids {
        assert!(!alive(pid), "pid {pid} survived cancel");
    }
    let second = ops.cancel(&op, Duration::from_millis(300)).await.unwrap();
    assert_eq!(second, first);
    assert_eq!(ops.status(&op).await.unwrap(), first);
}

#[tokio::test]
async fn timeout_settles_as_timed_out_with_the_final_output() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let script = "trap 'echo last words; exit 0' TERM; sleep 300 & wait";
    let op = ops
        .start(&workspace(), spec("k1", script, 1))
        .await
        .unwrap();
    let data = drain(&ops, &op, 4096).await;
    assert!(
        data.starts_with(b"last words\n"),
        "{}",
        String::from_utf8_lossy(&data)
    );
    assert_eq!(
        ops.status(&op).await.unwrap().state,
        OperationState::TimedOut
    );
}

#[tokio::test]
async fn unknown_ref_is_a_typed_unknown_operation() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let op = ops
        .start(&workspace(), spec("k1", "true", 60))
        .await
        .unwrap();
    let mut forged = op.clone();
    forged.operation_id = "op-forged".try_into().unwrap();
    for error in [
        ops.status(&forged).await.unwrap_err(),
        ops.cancel(&forged, Duration::ZERO).await.unwrap_err(),
    ] {
        assert_eq!(error.kind, ErrorKind::UnknownOperation);
    }
}

#[tokio::test]
async fn finalize_ends_running_operations_and_forgets_them() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let op = ops
        .start(&workspace(), spec("k1", "sleep 300", 60))
        .await
        .unwrap();
    let outcome = ops.finalize(&workspace()).await;
    assert_eq!(
        serde_json::to_value(&outcome).unwrap(),
        serde_json::json!({"state": "completed"})
    );
    assert_eq!(
        ops.status(&op).await.unwrap_err().kind,
        ErrorKind::UnknownOperation
    );
}

#[test]
fn credential_bindings_are_refused_until_a_resolver_exists() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let mut spec = serde_json::to_value(spec("k1", "true", 60)).unwrap();
    spec["credential_bindings"] = serde_json::json!([{
        "ref": {"name": "github-app", "scope": "repo:acme/widgets"},
        "delivery": "env",
        "target": "GH_TOKEN",
    }]);
    let spec: OperationSpec = serde_json::from_value(spec).unwrap();
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let error = runtime.block_on(ops.start(&workspace(), spec)).unwrap_err();
    assert_eq!(error.kind, ErrorKind::UnsupportedCapability);
    assert!(!dir.path().join("docker.log").exists());
}
