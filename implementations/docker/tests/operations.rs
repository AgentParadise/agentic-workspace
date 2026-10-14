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
    CredentialRef, CredentialResolver, ErrorKind, ObserveRequest, OperationError, OperationRef,
    OperationSpec, OperationState, OperationStatus, SupportsOperations, WorkspaceRef,
};
use agentic_workspace_docker::operations::DockerOperations;
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;
use std::future::Future;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

/// Write an executable script whose first line answers `--probe`, and wait
/// until it can be executed: a test thread forking meanwhile can hold the
/// written file open for a moment, and exec then fails with ETXTBSY.
fn write_executable(path: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::write(
        path,
        format!("#!/bin/sh\n[ \"$1\" = --probe ] && exit 0\n{body}"),
    )
    .unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    for _ in 0..500 {
        match std::process::Command::new(path).arg("--probe").status() {
            Err(e) if e.raw_os_error() == Some(26) => std::thread::sleep(Duration::from_millis(10)),
            status => {
                assert!(status.unwrap().success());
                return;
            }
        }
    }
    panic!("{} stayed busy", path.display());
}

/// `exec` drops docker's own flags and runs the command here; `stop` is
/// recorded. Every argv is appended to `docker.log` beside the script. While
/// a `fail` file sits beside it, signalling and `stop` both fail; `container
/// inspect` says the container is running, or gone while a `gone` file sits
/// beside it, or cannot reach the daemon while a `down` file does.
fn fake_docker(dir: &Path) -> PathBuf {
    let script = dir.join("docker");
    let log = dir.join("docker.log");
    let fail = dir.join("fail");
    let gone = dir.join("gone");
    let down = dir.join("down");
    write_executable(
        &script,
        &format!(
            r#"PATH='{dir}':"$PATH"; export PATH
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
    [ -e '{fail}' ] && [ "$4" = aw-signal ] && exit 1
    exec "$@" ;;
stop) [ -e '{fail}' ] && exit 1; exit 0 ;;
container)
    [ -e '{down}' ] && {{ echo 'Cannot connect to the Docker daemon' >&2; exit 1; }}
    [ -e '{gone}' ] && {{ echo 'Error: No such container: c1' >&2; exit 1; }}
    echo running ;;
esac
"#,
            log = log.display(),
            dir = dir.display(),
            fail = fail.display(),
            gone = gone.display(),
            down = down.display()
        ),
    );
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

/// A descendant holds the output open past the leader's exit and writes
/// later: once a page is complete, nothing more may ever be read.
#[tokio::test]
async fn a_complete_page_stays_the_end_when_a_descendant_writes_late() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let script = "(sleep 4; echo late; echo late >&2) & echo early";
    let op = ops
        .start(&workspace(), spec("k1", script, 60))
        .await
        .unwrap();
    let data = drain(&ops, &op, 4096).await;
    assert_eq!(data, b"early\n");
    let status = ops.status(&op).await.unwrap();
    assert!(
        status
            .metadata
            .get()
            .get("signal_failure")
            .is_some_and(|f| f.contains("stopped reading")),
        "{status:?}"
    );
    tokio::time::sleep(Duration::from_secs(4)).await;
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
    assert!(page.complete);
    assert_eq!(STANDARD.decode(page.data_b64.as_str()).unwrap(), b"early\n");
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

#[tokio::test]
async fn failed_cleanup_stays_resolvable_and_a_retry_cleans_up() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let op = ops
        .start(&workspace(), spec("k1", "echo $$; sleep 300", 60))
        .await
        .unwrap();
    let page = ops
        .observe(
            &op,
            ObserveRequest {
                cursor: None,
                max_bytes: 4096,
                wait: Duration::from_secs(5),
            },
        )
        .await
        .unwrap();
    let pid = String::from_utf8(STANDARD.decode(page.data_b64.as_str()).unwrap()).unwrap();
    let pid = pid.trim().to_owned();
    assert!(alive(&pid), "{pid:?}");

    std::fs::write(dir.path().join("fail"), "").unwrap();
    let error = ops
        .cancel(&op, Duration::from_millis(100))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::ProviderUnavailable);
    let outcome = serde_json::to_value(ops.finalize(&workspace()).await).unwrap();
    assert_eq!(outcome["state"], "failed", "{outcome}");
    assert_eq!(
        outcome["error"]["kind"], "provider_unavailable",
        "{outcome}"
    );
    assert!(alive(&pid));
    assert_eq!(
        ops.status(&op).await.unwrap().state,
        OperationState::Running
    );

    std::fs::remove_file(dir.path().join("fail")).unwrap();
    // Another provider's workspace with the same resource id is not ours.
    let foreign: WorkspaceRef =
        serde_json::from_value(serde_json::json!({"provider": "e2b", "resource_id": "c1"}))
            .unwrap();
    ops.finalize(&foreign).await;
    assert!(alive(&pid));
    assert_eq!(
        ops.status(&op).await.unwrap().state,
        OperationState::Running
    );

    let outcome = serde_json::to_value(ops.finalize(&workspace()).await).unwrap();
    assert_eq!(outcome, serde_json::json!({"state": "completed"}));
    assert!(!alive(&pid), "pid {pid} survived the retried finalize");
    assert_eq!(
        ops.status(&op).await.unwrap_err().kind,
        ErrorKind::UnknownOperation
    );
}

/// Start `echo $$; sleep 300` and return its leader's pid once printed.
async fn sleeper(ops: &DockerOperations) -> (OperationRef, String) {
    let op = ops
        .start(&workspace(), spec("k1", "echo $$; sleep 300", 60))
        .await
        .unwrap();
    let page = ops
        .observe(
            &op,
            ObserveRequest {
                cursor: None,
                max_bytes: 4096,
                wait: Duration::from_secs(5),
            },
        )
        .await
        .unwrap();
    let pid = String::from_utf8(STANDARD.decode(page.data_b64.as_str()).unwrap()).unwrap();
    (op, pid.trim().to_owned())
}

/// Poll `status` until `done` holds of its result.
async fn status_until(
    ops: &DockerOperations,
    op: &OperationRef,
    done: impl Fn(&Result<OperationStatus, OperationError>) -> bool,
) -> Result<OperationStatus, OperationError> {
    for _ in 0..100 {
        let status = ops.status(op).await;
        if done(&status) {
            return status;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("status never settled: {:?}", ops.status(op).await);
}

#[tokio::test]
async fn a_workload_whose_container_is_removed_is_lost() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = sleeper(&ops).await;
    // Removing the container kills its processes and then it is gone.
    std::fs::write(dir.path().join("gone"), "").unwrap();
    assert!(
        std::process::Command::new("kill")
            .args(["-KILL", "--", &format!("-{pid}")])
            .status()
            .unwrap()
            .success()
    );
    let status = status_until(&ops, &op, |s| {
        s.as_ref().is_ok_and(|s| s.state.is_terminal())
    })
    .await
    .unwrap();
    assert_eq!(status.state, OperationState::Lost);
}

#[tokio::test]
async fn an_unreachable_daemon_is_provider_unavailable_and_a_real_failure_stays_failed() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = sleeper(&ops).await;
    std::fs::write(dir.path().join("down"), "").unwrap();
    assert!(
        std::process::Command::new("kill")
            .args(["-KILL", "--", &format!("-{pid}")])
            .status()
            .unwrap()
            .success()
    );
    let error = status_until(&ops, &op, |s| s.is_err()).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::ProviderUnavailable, "{error:?}");
    // The daemon is back and the container is still there: the workload failed.
    std::fs::remove_file(dir.path().join("down")).unwrap();
    let status = ops.status(&op).await.unwrap();
    assert_eq!(status.state, OperationState::Failed);
}

/// Resolves every credential to `secret-for-<name>`.
struct Vault;

impl CredentialResolver for Vault {
    fn resolve<'a>(
        &'a self,
        credential: &'a CredentialRef,
    ) -> Pin<Box<dyn Future<Output = Result<String, OperationError>> + Send + 'a>> {
        Box::pin(async move { Ok(format!("secret-for-{}", credential.name.as_str())) })
    }
}

fn launches(dir: &Path) -> usize {
    std::fs::read_to_string(dir.join("docker.log"))
        .unwrap_or_default()
        .lines()
        .filter(|line| line.starts_with("exec -i "))
        .count()
}

#[tokio::test]
async fn credential_bindings_reach_the_workload_env_but_never_the_client_argv() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16).with_credentials(Arc::new(Vault));
    let mut spec = serde_json::to_value(spec("k1", "echo \"$GH_TOKEN\"", 60)).unwrap();
    spec["credential_bindings"] = serde_json::json!([{
        "ref": {"name": "github-app", "scope": "repo:acme/widgets"},
        "delivery": "env",
        "target": "GH_TOKEN",
    }]);
    let spec: OperationSpec = serde_json::from_value(spec).unwrap();
    let op = ops.start(&workspace(), spec).await.unwrap();
    assert_eq!(drain(&ops, &op, 4096).await, b"secret-for-github-app\n");
    let log = std::fs::read_to_string(dir.path().join("docker.log")).unwrap();
    assert!(log.contains(" -e GH_TOKEN "), "{log}");
    assert!(!log.contains("secret-for"), "{log}");
    let status = serde_json::to_string(&ops.status(&op).await.unwrap()).unwrap();
    assert!(!status.contains("secret-for"), "{status}");
}

/// Drive a shared APSS behaviour fixture, step by step from its file, against
/// `DockerOperations` over the stand-in docker, with the fixture's own specs
/// and credential bindings. Each step checks what the fixture asserts about
/// behaviour: the issued ref (same on a same-key replay), an error's `kind`,
/// the status `state` and `exit_code`, and the launch count. Provider-chosen
/// ids, messages, timestamps and metadata are not compared; the Python
/// provider does not reproduce them either.
async fn drive_behaviour_fixture(name: &str) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../lib/python/agentic_isolation/tests/fixtures/apss_exp_v1_0006/behaviour")
        .join(name);
    let fixture: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let agent = dir.path().join("agent");
    write_executable(&agent, "exec sleep 300\n");
    let ops = ops(dir.path(), 1 << 20, 1 << 16).with_credentials(Arc::new(Vault));
    let mut issued: Option<(Value, OperationRef)> = None;
    for (i, step) in fixture["steps"].as_array().unwrap().iter().enumerate() {
        let at = format!("{name}#{i} {}", step["call"]);
        let expect = &step["expect"]["document"];
        // A ref the fixture names is the one the provider issued for it.
        let reference = || -> OperationRef {
            match &issued {
                Some((named, ours)) if *named == step["args"]["ref"] => ours.clone(),
                _ => serde_json::from_value(step["args"]["ref"].clone()).unwrap(),
            }
        };
        let outcome: Result<Value, OperationError> = match step["call"].as_str().unwrap() {
            "start" => {
                let spec: OperationSpec =
                    serde_json::from_value(step["args"]["spec"].clone()).unwrap();
                let workspace: WorkspaceRef = serde_json::from_value(
                    serde_json::json!({"provider": "docker", "resource_id": "ws-7f3a"}),
                )
                .unwrap();
                ops.start(&workspace, spec).await.map(|op| {
                    if expect.get("kind").is_none() {
                        match &issued {
                            None => issued = Some((expect.clone(), op.clone())),
                            Some((_, first)) => assert_eq!(&op, first, "{at}: a second ref"),
                        }
                    }
                    serde_json::to_value(op).unwrap()
                })
            }
            "status" => ops
                .status(&reference())
                .await
                .map(|s| serde_json::to_value(s).unwrap()),
            "cancel" => ops
                .cancel(&reference(), Duration::from_millis(300))
                .await
                .map(|s| serde_json::to_value(s).unwrap()),
            call => panic!("{at}: the driver does not dispatch {call:?}"),
        };
        match (expect.get("kind"), outcome) {
            (Some(kind), Err(error)) => {
                assert_eq!(&serde_json::to_value(error.kind).unwrap(), kind, "{at}")
            }
            (Some(kind), Ok(document)) => panic!("{at}: expected {kind}, got {document}"),
            (None, Err(error)) => panic!("{at}: {error:?}"),
            (None, Ok(document)) => {
                for key in ["workspace", "operation_key"] {
                    if let Some(want) = expect.get(key) {
                        assert_eq!(document.get(key), Some(want), "{at}: {key}");
                    }
                }
                for key in ["state", "exit_code"] {
                    if let Some(want) = expect.get(key) {
                        assert_eq!(
                            document.get(key).unwrap_or(&Value::Null),
                            want,
                            "{at}: {key}"
                        );
                    }
                }
            }
        }
        // The stand-in logs a launch once its process runs; let it, then count.
        let want = step["launches"].as_u64().unwrap() as usize;
        for _ in 0..50 {
            if launches(dir.path()) >= want {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(launches(dir.path()), want, "{at}: launches");
    }
    ops.finalize(
        &serde_json::from_value(
            serde_json::json!({"provider": "docker", "resource_id": "ws-7f3a"}),
        )
        .unwrap(),
    )
    .await;
}

#[tokio::test]
async fn behaviour_fixture_start_same_key_one_launch() {
    drive_behaviour_fixture("start-same-key-one-launch.json").await;
}

#[tokio::test]
async fn behaviour_fixture_same_key_different_spec_conflict() {
    drive_behaviour_fixture("same-key-different-spec-conflict.json").await;
}

#[tokio::test]
async fn behaviour_fixture_cancel_twice_terminal_once() {
    drive_behaviour_fixture("cancel-twice-terminal-once.json").await;
}

#[tokio::test]
async fn behaviour_fixture_unknown_ref_typed_error() {
    drive_behaviour_fixture("unknown-ref-typed-error.json").await;
}

#[test]
fn credential_bindings_without_a_resolver_are_rejected_policy() {
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
    assert_eq!(error.kind, ErrorKind::RejectedPolicy);
    assert!(!dir.path().join("docker.log").exists());
}
