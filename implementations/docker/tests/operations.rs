//! The APSS 0.2.0 behaviour fixtures, as named provider tests, against
//! `DockerOperations` driven through a stand-in `docker` whose `exec` runs
//! the command here. That exercises the real wrapper, `setsid`, `/proc` and
//! signal scripts; what it cannot prove (namespaces, `docker stop`) is in the
//! docker-backed `conformance` test.
//!
//! The stand-in needs `setsid` and `/proc`, so the suite is Linux only; the
//! Linux `rust-test` job runs it.
#![cfg(target_os = "linux")]

use agentic_workspace_conformance::behaviour::{
    BehaviourHarness, Workload, behaviour_fixture, drive,
};
use agentic_workspace_core::operations::{
    CredentialRef, CredentialResolver, ErrorKind, FinalizationState, ObserveRequest,
    OperationError, OperationRef, OperationSpec, OperationState, OperationStatus,
    SupportsOperations, WorkspaceRef,
};
use agentic_workspace_docker::operations::DockerOperations;
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
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

/// `exec` drops docker's own flags and runs the command here, marking every
/// process it starts as the container's with `AW_STANDIN=<dir>` in its
/// environment. Every argv is appended to `docker.log` beside the script.
///
/// The process table is the container's: the host's init is not its init,
/// so a host pid 1 parent reads as 0, and while a `reaper` file names a
/// subreaper pid, that pid's adoptees read as children of pid 1.
///
/// While a `fail` file sits beside it, signalling and `stop` both fail; a
/// `signal-fails` file fails signalling only. `stop` KILLs every marked
/// process and leaves the container `exited`, unless a `stop-lies` file
/// makes it report success and do nothing, or a `stop-keeps-running` file
/// leaves the container reported running after the KILLs. `container inspect` says the
/// container is running or exited, or gone while a `gone` file sits beside
/// it, or cannot reach the daemon while a `down` file does, or is stuck
/// `removing` while a `removing` file does. While a `late-exec` file sits
/// beside it, an operation's `exec` waits 3s before it runs anything, so its
/// launch header comes late. Every `exec` is marked as the container's from
/// its start, so `stop` kills a held-back one too; once the container is
/// stopped, an `exec` is refused with 137, as a stopped container's would be.
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
    AW_STANDIN='{dir}'; export AW_STANDIN
    [ -e '{dir}/late-exec' ] && case " $* " in *" aw-operation "*) sleep 3 ;; esac
    [ -e '{dir}/stopped' ] && exit 137
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
    [ -e '{fail}' ] || [ -e '{dir}/signal-fails' ] && [ "$4" = aw-signal ] && exit 1
    if [ "$4" = aw-processes ]; then
        reaper=$(cat '{dir}/reaper' 2>/dev/null)
        "$@" | awk -v r="$reaper" '{{ if ($2 == 1) $2 = 0; else if (r != "" && $2 == r) $2 = 1; print }}'
        exit 0
    fi
    exec "$@" ;;
stop)
    [ -e '{fail}' ] && exit 1
    [ -e '{dir}/stop-lies' ] && exit 0
    for e in /proc/[0-9]*/environ; do
        tr '\0' '\n' < "$e" 2>/dev/null | grep -qx 'AW_STANDIN={dir}' || continue
        p=${{e#/proc/}}; kill -KILL "${{p%/environ}}" 2>/dev/null
    done
    [ -e '{dir}/stop-keeps-running' ] || : > '{dir}/stopped'; exit 0 ;;
container)
    [ -e '{down}' ] && {{ echo 'Cannot connect to the Docker daemon' >&2; exit 1; }}
    [ -e '{gone}' ] && {{ echo 'Error: No such container: c1' >&2; exit 1; }}
    [ -e '{dir}/removing' ] && {{ echo removing; exit 0; }}
    [ -e '{dir}/stopped' ] && {{ echo exited; exit 0; }}
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
    assert!(data.ends_with(b"tail") && !page.complete);
    let end = ops
        .observe(
            &op,
            ObserveRequest {
                cursor: Some(page.next_cursor.to_string()),
                max_bytes: 4096,
                wait: Duration::ZERO,
            },
        )
        .await
        .unwrap();
    assert!(end.data_b64.as_str().is_empty() && end.complete);
}

#[tokio::test]
async fn late_read_drains_bounded_pages_then_completes() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16).with_separate_channels();
    let op = ops
        .start(&workspace(), spec("k1", "seq 1 3000; echo done >&2", 60))
        .await
        .unwrap();
    let data = String::from_utf8(drain(&ops, &op, 300).await).unwrap();
    let stdout: String = (1..=3000).map(|n| n.to_string() + "\n").collect();
    // Each channel is whole and in order. The two pipes are read
    // independently, so the combined stream may carry stderr's chunk at any
    // point between stdout's: only its presence is asserted there.
    let (out, err) = ops.channels(&op).unwrap();
    assert_eq!(
        (out.as_slice(), err.as_slice()),
        (stdout.as_bytes(), &b"done\n"[..])
    );
    assert_eq!(data.len(), stdout.len() + 5);
    assert_eq!(data.replacen("done\n", "", 1), stdout);
}

/// Every page until `complete`, each checked against the provider's own cap
/// whatever the caller asked for; the pages read.
async fn drain_capped(
    ops: &DockerOperations,
    op: &OperationRef,
    max_bytes: usize,
    cap: usize,
) -> (Vec<u8>, usize) {
    let (mut cursor, mut data) = (None, Vec::new());
    for pages in 1..10_000 {
        let request = ObserveRequest {
            cursor: cursor.clone(),
            max_bytes,
            wait: Duration::from_secs(5),
        };
        let page = ops.observe(op, request).await.unwrap();
        let size = serde_json::to_vec(&page).unwrap().len();
        assert!(
            size <= cap,
            "page {pages} is {size} bytes, over the cap {cap}"
        );
        data.extend(STANDARD.decode(page.data_b64.as_str()).unwrap());
        if page.complete {
            return (data, pages);
        }
        cursor = Some(page.next_cursor.to_string());
    }
    panic!("never complete");
}

/// The provider's `max_page_bytes` bounds every page, for a caller asking
/// for no limit (0) and for one asking for more than the cap.
#[tokio::test]
async fn the_provider_page_cap_bounds_every_page_below_the_callers_limit() {
    const CAP: usize = 400;
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, CAP as u64);
    let op = ops
        .start(&workspace(), spec("k1", "seq 1 500", 60))
        .await
        .unwrap();
    let expected: String = (1..=500).map(|n| n.to_string() + "\n").collect();
    for max_bytes in [0, 4096] {
        let (data, pages) = drain_capped(&ops, &op, max_bytes, CAP).await;
        assert_eq!(
            String::from_utf8(data).unwrap(),
            expected,
            "max_bytes {max_bytes}"
        );
        assert!(pages > 2, "{pages} pages for {} bytes", expected.len());
    }
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
            .get("output_failure")
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
    assert!(!page.complete);
    assert_eq!(STANDARD.decode(page.data_b64.as_str()).unwrap(), b"early\n");
    let end = ops
        .observe(
            &op,
            ObserveRequest {
                cursor: Some(page.next_cursor.to_string()),
                max_bytes: 4096,
                wait: Duration::ZERO,
            },
        )
        .await
        .unwrap();
    assert!(end.data_b64.as_str().is_empty() && end.complete);
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

unsafe extern "C" {
    fn prctl(option: i32, ...) -> i32;
}

/// Make this test process the subreaper of everything it starts, so an
/// orphan is adopted here, where the stand-in can present it as adopted by
/// the container's init (see `fake_docker`).
fn become_subreaper(dir: &Path) {
    const PR_SET_CHILD_SUBREAPER: i32 = 36;
    // SAFETY: prctl with integer arguments only.
    assert_eq!(
        unsafe { prctl(PR_SET_CHILD_SUBREAPER, 1u64, 0u64, 0u64, 0u64) },
        0
    );
    std::fs::write(dir.join("reaper"), std::process::id().to_string()).unwrap();
}

/// Read whitespace-separated pids from the output until `count` have come.
async fn printed_pids(ops: &DockerOperations, op: &OperationRef, count: usize) -> Vec<String> {
    let (mut pids, mut cursor) = (Vec::new(), None);
    while pids.len() < count {
        let page = ops
            .observe(
                op,
                ObserveRequest {
                    cursor,
                    max_bytes: 4096,
                    wait: Duration::from_secs(5),
                },
            )
            .await
            .unwrap();
        assert!(!page.complete, "ended before cancel");
        let text = String::from_utf8(STANDARD.decode(page.data_b64.as_str()).unwrap()).unwrap();
        pids.extend(
            text.split_whitespace()
                .filter(|w| w.parse::<u32>().is_ok())
                .map(String::from),
        );
        cursor = Some(page.next_cursor.to_string());
    }
    pids
}

fn parent_of(pid: &str) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(") ")?
        .1
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}

/// A double fork: the workload's child leaves the group and exits at once,
/// so its own child is adopted by init before cancel first looks, with no
/// parent chain back to the operation. Signalling the group cannot confirm
/// it gone; cancel must fail closed into a confirmed container stop.
#[tokio::test]
async fn cancel_stops_the_container_for_a_double_forked_orphan_of_init() {
    let dir = tempfile::tempdir().unwrap();
    become_subreaper(dir.path());
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let script = "setsid sh -c 'sleep 300 & echo orphan $!' & wait; echo leader $$; sleep 300";
    let op = ops
        .start(&workspace(), spec("k1", script, 60))
        .await
        .unwrap();
    let pids = printed_pids(&ops, &op, 2).await;
    let orphan = &pids[0];
    // Its parent has exited and init (here, this subreaper) adopted it.
    for _ in 0..100 {
        if parent_of(orphan) == Some(std::process::id()) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(parent_of(orphan), Some(std::process::id()), "not adopted");
    assert!(pids.iter().all(|p| alive(p)), "{pids:?}");
    let status = ops.cancel(&op, Duration::from_millis(300)).await.unwrap();
    assert_eq!(status.state, OperationState::Cancelled);
    let metadata = status.metadata.get();
    assert_eq!(
        metadata.get("escalation").map(String::as_str),
        Some("container_stopped"),
        "{status:?}"
    );
    assert!(
        metadata["signal_failure"].contains(&format!("\"{orphan}\"")),
        "{status:?}"
    );
    for pid in &pids {
        assert!(!alive(pid), "pid {pid} survived cancel");
    }
    assert!(dir.path().join("stopped").exists());
}

/// Start a sleeper whose signalling fails, so cancel escalates to `stop`.
async fn unsignallable(dir: &Path, ops: &DockerOperations) -> (OperationRef, String) {
    let (op, pid) = sleeper(ops).await;
    std::fs::write(dir.join("signal-fails"), "").unwrap();
    (op, pid)
}

#[tokio::test]
async fn an_escalated_cancel_is_confirmed_by_the_stopped_container() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = unsignallable(dir.path(), &ops).await;
    let status = ops.cancel(&op, Duration::from_millis(100)).await.unwrap();
    assert_eq!(status.state, OperationState::Cancelled, "{status:?}");
    assert!(status.metadata.get().contains_key("escalation"));
    assert!(!alive(&pid), "the leader survived the stop");
}

/// `docker stop` exits 0 but the container keeps running: the cancel is
/// unconfirmed, never `cancelled`, and a retry once stop works confirms it.
#[tokio::test]
async fn a_stop_that_falsely_reports_success_never_certifies_cancellation() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = unsignallable(dir.path(), &ops).await;
    std::fs::write(dir.path().join("stop-lies"), "").unwrap();
    let error = ops
        .cancel(&op, Duration::from_millis(100))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::ProviderUnavailable, "{error:?}");
    assert!(
        error.message.as_str().contains("still running"),
        "{error:?}"
    );
    assert!(alive(&pid));
    assert_eq!(
        ops.status(&op).await.unwrap().state,
        OperationState::Running
    );
    std::fs::remove_file(dir.path().join("stop-lies")).unwrap();
    let status = ops.cancel(&op, Duration::from_millis(100)).await.unwrap();
    assert_eq!(status.state, OperationState::Cancelled, "{status:?}");
    assert!(!alive(&pid));
}

/// The stop KILLs the workload, so its client exits and the collector
/// settles while the cancel is still confirming, but the container is then
/// reported running: settlement must not publish `cancelled`.
#[tokio::test]
async fn a_collector_settling_during_an_unconfirmed_cancel_never_publishes_cancelled() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = unsignallable(dir.path(), &ops).await;
    std::fs::write(dir.path().join("stop-keeps-running"), "").unwrap();
    let error = ops
        .cancel(&op, Duration::from_millis(100))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::ProviderUnavailable, "{error:?}");
    assert!(!alive(&pid), "the stop itself ran");
    let status = status_until(&ops, &op, |s| {
        s.as_ref().is_ok_and(|s| s.state.is_terminal())
    })
    .await
    .unwrap();
    assert_eq!(status.state, OperationState::Failed, "{status:?}");
}

/// The stop ran but the daemon cannot then report the container: the
/// termination is unconfirmed, so the outcome is never `cancelled` while it
/// stays so, and a retried cancel confirms it.
#[tokio::test]
async fn an_unreadable_state_after_stop_never_certifies_cancellation() {
    let dir = tempfile::tempdir().unwrap();
    let ops = ops(dir.path(), 1 << 20, 1 << 16);
    let (op, pid) = unsignallable(dir.path(), &ops).await;
    std::fs::write(dir.path().join("down"), "").unwrap();
    let error = ops
        .cancel(&op, Duration::from_millis(100))
        .await
        .unwrap_err();
    assert_eq!(error.kind, ErrorKind::ProviderUnavailable, "{error:?}");
    assert!(!alive(&pid), "the stop itself ran");
    for _ in 0..30 {
        match ops.status(&op).await {
            Ok(status) => assert_ne!(status.state, OperationState::Cancelled),
            Err(e) => assert_eq!(e.kind, ErrorKind::ProviderUnavailable),
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    std::fs::remove_file(dir.path().join("down")).unwrap();
    let status = ops.cancel(&op, Duration::from_millis(100)).await.unwrap();
    assert_eq!(status.state, OperationState::Cancelled, "{status:?}");
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

/// Resolves `slow` only once released, announcing that it has been entered;
/// every other credential at once.
struct Gate {
    entered: tokio::sync::Notify,
    release: tokio::sync::Semaphore,
}

impl Default for Gate {
    fn default() -> Self {
        Self {
            entered: tokio::sync::Notify::new(),
            release: tokio::sync::Semaphore::new(0),
        }
    }
}

impl CredentialResolver for Gate {
    fn resolve<'a>(
        &'a self,
        credential: &'a CredentialRef,
    ) -> Pin<Box<dyn Future<Output = Result<String, OperationError>> + Send + 'a>> {
        Box::pin(async move {
            if credential.name.as_str() == "slow" {
                self.entered.notify_one();
                self.release.acquire().await.unwrap().forget();
            }
            Ok("value".to_owned())
        })
    }
}

fn with_credential(spec: OperationSpec, name: &str) -> OperationSpec {
    let mut spec = serde_json::to_value(spec).unwrap();
    spec["credential_bindings"] = serde_json::json!([{
        "ref": {"name": name, "scope": "repo:acme/widgets"},
        "delivery": "env",
        "target": "GH_TOKEN",
    }]);
    serde_json::from_value(spec).unwrap()
}

fn workspace_named(container: &str) -> WorkspaceRef {
    serde_json::from_value(serde_json::json!({"provider": "docker", "resource_id": container}))
        .unwrap()
}

/// Fails the test if `future` is blocked; a deadlock detector, not a proof
/// of ordering: the resolver's gate is what holds the slow start.
async fn unblocked<T>(what: &str, future: impl Future<Output = T>) -> T {
    tokio::time::timeout(Duration::from_secs(10), future)
        .await
        .unwrap_or_else(|_| panic!("{what} blocked behind a pending credential resolution"))
}

/// While one key's start awaits its credential, other keys, other
/// workspaces and existing operations proceed; the slow start completes
/// once released, and same-key starts still launch once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_pending_credential_resolution_blocks_only_its_own_key() {
    let dir = tempfile::tempdir().unwrap();
    let gate = Arc::new(Gate::default());
    let ops = Arc::new(ops(dir.path(), 1 << 20, 1 << 16).with_credentials(gate.clone()));
    let (existing, _) = sleeper(&ops).await;
    let slow_spec = with_credential(spec("slow", "echo slow", 60), "slow");
    let slow = tokio::spawn({
        let (ops, spec) = (ops.clone(), slow_spec.clone());
        async move { ops.start(&workspace(), spec).await }
    });
    unblocked("the slow start", gate.entered.notified()).await;
    // A same-key start waits behind the first; it must not launch again.
    let replay = tokio::spawn({
        let ops = ops.clone();
        async move { ops.start(&workspace(), slow_spec).await }
    });
    let other_key = unblocked(
        "another key",
        ops.start(&workspace(), spec("k2", "echo k2", 60)),
    )
    .await
    .unwrap();
    let other_workspace = unblocked(
        "another workspace",
        ops.start(&workspace_named("c2"), spec("slow", "echo c2", 60)),
    )
    .await
    .unwrap();
    assert_eq!(drain(&ops, &other_key, 4096).await, b"k2\n");
    assert_eq!(drain(&ops, &other_workspace, 4096).await, b"c2\n");
    let status = unblocked("status", ops.status(&existing)).await.unwrap();
    assert_eq!(status.state, OperationState::Running);
    let request = ObserveRequest {
        cursor: None,
        max_bytes: 4096,
        wait: Duration::ZERO,
    };
    unblocked("observe", ops.observe(&existing, request))
        .await
        .unwrap();
    let cancelled = unblocked("cancel", ops.cancel(&existing, Duration::from_millis(100)))
        .await
        .unwrap();
    assert_eq!(cancelled.state, OperationState::Cancelled);
    assert!(!slow.is_finished() && !replay.is_finished());
    gate.release.add_permits(1);
    let slow = unblocked("the released start", slow)
        .await
        .unwrap()
        .unwrap();
    let replay = unblocked("the replay", replay).await.unwrap().unwrap();
    assert_eq!(slow, replay);
    assert_eq!(drain(&ops, &slow, 4096).await, b"slow\n");
    // existing, the slow key, another key and another workspace: four launches.
    assert_eq!(launches(dir.path()), 4);
}

/// A finalize racing a start whose credential is pending waits for that
/// launch, then ends and forgets it; a start arriving meanwhile is refused.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn finalize_waits_for_an_in_flight_start_and_ends_it() {
    let dir = tempfile::tempdir().unwrap();
    let gate = Arc::new(Gate::default());
    let ops = Arc::new(ops(dir.path(), 1 << 20, 1 << 16).with_credentials(gate.clone()));
    let slow = tokio::spawn({
        let ops = ops.clone();
        let spec = with_credential(spec("slow", "sleep 300", 60), "slow");
        async move { ops.start(&workspace(), spec).await }
    });
    unblocked("the slow start", gate.entered.notified()).await;
    let finalize = tokio::spawn({
        let ops = ops.clone();
        async move { ops.finalize(&workspace()).await }
    });
    // Refused once the finalize has marked the workspace; polled until then.
    let refused = unblocked("a start during finalize", async {
        for n in 0.. {
            let late = spec(&format!("late{n}"), "true", 60);
            if let Err(e) = ops.start(&workspace(), late).await {
                return e;
            }
            tokio::task::yield_now().await;
        }
        unreachable!()
    })
    .await;
    assert_eq!(refused.kind, ErrorKind::UnknownOperation, "{refused:?}");
    assert!(
        !finalize.is_finished(),
        "finalize ignored the start in flight"
    );
    gate.release.add_permits(1);
    let slow = unblocked("the released start", slow)
        .await
        .unwrap()
        .unwrap();
    let outcome = unblocked("finalize", finalize).await.unwrap();
    assert_eq!(outcome.state, FinalizationState::Completed, "{outcome:?}");
    let error = ops.status(&slow).await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::UnknownOperation);
}

/// The stand-in docker as a [`BehaviourHarness`]: the fixture's `agent` waits
/// for a `release` file, writes the programmed output, then exits 0 or keeps
/// running. Retention and page bound are the published Docker descriptor's.
struct StandIn {
    dir: tempfile::TempDir,
    ops: DockerOperations,
}

impl StandIn {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let ops = ops(dir.path(), 8 << 20, 1 << 16).with_credentials(Arc::new(Vault));
        Self { dir, ops }
    }
}

impl BehaviourHarness for StandIn {
    type Provider = DockerOperations;

    fn provider(&self) -> &DockerOperations {
        &self.ops
    }

    fn program(&self, workload: &Workload) {
        let payload = self.dir.path().join("payload");
        std::fs::write(&payload, &workload.output).unwrap();
        let release = self.dir.path().join("release");
        let end = if workload.exits {
            "exit 0"
        } else {
            "exec sleep 300"
        };
        write_executable(
            &self.dir.path().join("agent"),
            &format!(
                "while [ ! -e '{}' ]; do sleep 0.05; done\ncat '{}'\n{end}\n",
                release.display(),
                payload.display()
            ),
        );
    }

    fn release(&self) {
        std::fs::write(self.dir.path().join("release"), b"").unwrap();
    }

    fn launches(&self) -> usize {
        launches(self.dir.path())
    }
}

async fn drive_behaviour_fixture(name: &str) {
    let fixture = behaviour_fixture(name);
    drive(name, &fixture, &StandIn::new()).await;
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

#[tokio::test]
async fn behaviour_fixture_cursor_after_retention_gap() {
    drive_behaviour_fixture("cursor-after-retention-gap.json").await;
}

#[tokio::test]
async fn behaviour_fixture_cursor_after_retention_gap_after_unrelated_operations() {
    // Unrelated operations widen every later generation, and with it the
    // page envelope; the fixture's pages must stay reachable at each width.
    for total in [0x1_0000, 0x10_0000] {
        for _ in 0..total {
            drop(agentic_workspace_core::operations::OperationSpool::new(
                NonZeroU64::MIN,
            ));
        }
        drive_behaviour_fixture("cursor-after-retention-gap.json").await;
    }
}

#[tokio::test]
async fn behaviour_fixture_late_read_drains_then_complete() {
    drive_behaviour_fixture("late-read-drains-then-complete.json").await;
}

#[tokio::test]
async fn behaviour_fixture_required_capture_unknown_success_false() {
    drive_behaviour_fixture("required-capture-unknown-success-false.json").await;
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

// --- APSS 0.2.1 lifecycle hazards (LH-1..LH-7) -------------------------------

mod hazards {
    use super::*;
    use agentic_workspace_conformance::hazard::{
        HazardFixture, HazardHarness, HazardVerdict, Outcome, Unmeasurable, drive_hazard,
        live_processes, load_hazards, vendored_behaviour_dir,
    };
    use serde_json::Value;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const TOKEN: &str = "AW_HARNESS_RUN";

    /// What each hazard's workload does once the provider launches it.
    fn workload(hazard: &str) -> &'static str {
        match hazard {
            // Double fork: the middle shell exits, so the setsid'd sleeper is
            // reparented and leaves the leader's group before cancel.
            "LH-1" => "(setsid sleep 300 </dev/null >/dev/null 2>&1 &); exec sleep 300",
            "LH-2" => "trap '' TERM; while :; do sleep 0.1; done",
            "LH-6" => "printf 'lost-it retained output\\n'",
            // The stand-in ignores `-w`; its marker names the "container".
            // `ws` is the file surface's workspace root; see `StandIn::file`.
            "LH-7" => {
                "mkdir -p \"$AW_STANDIN/ws/outputs\" && mkfifo \"$AW_STANDIN/ws/outputs/pipe\""
            }
            _ => "exec sleep 300",
        }
    }

    fn fixture(hazard: &str) -> HazardFixture {
        load_hazards(&vendored_behaviour_dir())
            .unwrap()
            .into_iter()
            .find(|f| f.hazard == hazard)
            .unwrap()
    }

    /// `DockerOperations` behind the stand-in docker, its workloads tokened.
    pub(super) struct StandIn<P> {
        pub provider: P,
        pub dir: tempfile::TempDir,
        pub token: String,
        gate: Arc<Gate>,
        key: std::sync::Mutex<Option<String>>,
    }

    /// LH-4's credential resolver: the first resolve, while armed, says it
    /// has begun and waits to be released; every resolve then answers as
    /// `Vault` does.
    #[derive(Default)]
    struct Gate {
        armed: std::sync::atomic::AtomicBool,
        entered: tokio::sync::Notify,
        release: tokio::sync::Notify,
    }

    struct GatedVault(Arc<Gate>);

    impl CredentialResolver for GatedVault {
        fn resolve<'a>(
            &'a self,
            credential: &'a CredentialRef,
        ) -> Pin<Box<dyn Future<Output = Result<String, OperationError>> + Send + 'a>> {
            Box::pin(async move {
                if self.0.armed.swap(false, Ordering::SeqCst) {
                    self.0.entered.notify_one();
                    self.0.release.notified().await;
                }
                Vault.resolve(credential).await
            })
        }
    }

    impl StandIn<DockerOperations> {
        pub fn new(hazard: &str) -> Self {
            StandIn::with(hazard, |ops| ops)
        }
    }

    impl<P> StandIn<P> {
        pub fn with(hazard: &str, wrap: impl FnOnce(DockerOperations) -> P) -> Self {
            let dir = tempfile::tempdir().unwrap();
            std::fs::create_dir_all(dir.path().join("repo")).unwrap();
            // This process stands for the container's init, so a detached
            // descendant reparents to "pid 1" as it would in a container.
            become_subreaper(dir.path());
            if hazard == "LH-3" {
                std::fs::write(dir.path().join("late-exec"), "").unwrap();
            }
            let gate = Arc::new(Gate::default());
            gate.armed.store(hazard == "LH-4", Ordering::SeqCst);
            // LH-6's provider descriptor: the 16-byte retention the gap needs.
            let retention = if hazard == "LH-6" { 16 } else { 1 << 20 };
            let provider = ops(dir.path(), retention, 1 << 16)
                .with_credentials(Arc::new(GatedVault(gate.clone())));
            let token = format!("rust-lh-{}-{}", std::process::id(), unique());
            Self {
                provider: wrap(provider),
                dir,
                token,
                gate,
                key: std::sync::Mutex::new(None),
            }
        }

        fn standin(&self, file: &str) -> PathBuf {
            self.dir.path().join(file)
        }
    }

    fn unique() -> usize {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        NEXT.fetch_add(1, Ordering::Relaxed)
    }

    impl<P> Drop for StandIn<P> {
        fn drop(&mut self) {
            // Only this run's tokened processes: the workload and what it forked.
            for pid in live_processes(TOKEN, &self.token).unwrap_or_default() {
                let _ = std::process::Command::new("kill")
                    .args(["-KILL", &pid.to_string()])
                    .status();
            }
            // A file-surface read still blocked opening LH-7's FIFO is let go:
            // a writer that opens and closes gives it end of file.
            use std::os::unix::fs::OpenOptionsExt;
            const O_NONBLOCK: i32 = 0o4000;
            let _ = std::fs::OpenOptions::new()
                .write(true)
                .custom_flags(O_NONBLOCK)
                .open(self.standin("ws/outputs/pipe"));
            self.gate.release.notify_one();
        }
    }

    /// How a `WorkspaceError` from the file surface reads as an APSS kind:
    /// a refused path is policy, anything else the provider's own failure.
    fn file_error(error: &agentic_workspace_core::WorkspaceError) -> Outcome {
        use agentic_workspace_core::WorkspaceError as E;
        let kind = match error {
            E::UnsafePath(_) | E::InvalidHandle(_) => "rejected_policy",
            _ => "internal",
        };
        Outcome::Error {
            kind: kind.into(),
            has_ref: false,
        }
    }

    impl<P: SupportsOperations> HazardHarness for StandIn<P> {
        type Provider = P;
        fn provider(&self) -> &P {
            &self.provider
        }
        fn workspace(&self) -> WorkspaceRef {
            workspace()
        }
        fn spec(&self, hazard: &str, fixture_spec: &Value) -> OperationSpec {
            let mut spec = fixture_spec.clone();
            spec["argv"] = serde_json::json!(["sh", "-c", workload(hazard)]);
            spec["environment"][TOKEN] = Value::String(self.token.clone());
            if hazard == "LH-5" {
                spec["timeout_seconds"] = 1.into();
            }
            if hazard == "LH-6" {
                // The fixture's offsets count the workload's output only.
                spec.as_object_mut().unwrap().remove("launch_marker");
            }
            *self.key.lock().unwrap() = spec["operation_key"].as_str().map(str::to_owned);
            serde_json::from_value(spec).unwrap()
        }
        fn establish(&self, hazard: &str, given: &str) -> Result<(), String> {
            match given {
                // The workload forks it; liveness is measured, not assumed.
                "detached_descendant" | "timeout_elapsed" => Ok(()),
                "observation_gap" if hazard == "LH-6" => Ok(()),
                // `late-exec` holds the exec back, so no workload runs yet.
                "launch_header_not_seen" => match live_processes(TOKEN, &self.token) {
                    Ok(live) if live.is_empty() => Ok(()),
                    Ok(live) => Err(format!("the workload already runs: {live:?}")),
                    Err(unmeasurable) => Err(unmeasurable.to_string()),
                },
                // Made true by `alongside`, while the start resolves.
                "destroyed_during_credential_resolution" if hazard == "LH-4" => Ok(()),
                "workspace_destroyed" if self.standin("gone").exists() => Ok(()),
                "backend_stuck_removing" => {
                    std::fs::write(self.standin("removing"), "").map_err(|e| e.to_string())
                }
                "fifo_in_workspace" => {
                    use std::os::unix::fs::FileTypeExt;
                    match std::fs::symlink_metadata(self.standin("ws/outputs/pipe")) {
                        Ok(meta) if meta.file_type().is_fifo() => Ok(()),
                        other => Err(format!("outputs/pipe is not a FIFO: {other:?}")),
                    }
                }
                other => Err(format!("the stand-in docker cannot produce {other}")),
            }
        }
        async fn alongside(&self, _hazard: &str, given: &str) -> Result<(), String> {
            if given != "destroyed_during_credential_resolution" {
                return Ok(());
            }
            let entered =
                tokio::time::timeout(Duration::from_secs(5), self.gate.entered.notified()).await;
            if entered.is_err() {
                self.gate.release.notify_one();
                return Err("start never reached credential resolution".into());
            }
            // The backend loses the container and the workspace is torn
            // down through the trait's boundary while start still resolves.
            std::fs::write(self.standin("gone"), "").map_err(|e| e.to_string())?;
            let destroyed = workspace();
            let finalize = self.provider.finalize(&destroyed);
            tokio::pin!(finalize);
            let early = tokio::time::timeout(Duration::from_millis(200), &mut finalize).await;
            self.gate.release.notify_one();
            if early.is_err() {
                finalize.await;
            }
            Ok(())
        }
        /// The Rust Docker provider's file surface is `WorkspaceProvider::
        /// collect`, read here on a thread of its own so a read that never
        /// returns is bounded by the step, not this test.
        async fn file(&self, call: &str, path: &str) -> Result<Outcome, String> {
            use agentic_workspace_core::{WorkspaceHandle, WorkspaceProvider};
            use agentic_workspace_docker::NetworkPolicy;
            let files = agentic_workspace_docker::DockerProvider::new(
                self.dir.path(),
                "stand-in",
                NetworkPolicy::None,
            )
            .map_err(|e| e.to_string())?;
            let root = std::fs::canonicalize(self.standin("ws")).map_err(|e| e.to_string())?;
            let handle = WorkspaceHandle {
                id: "ws".into(),
                working_directory: root.clone(),
                root,
                provider_reference: Some("agentic-workspace-ws".into()),
            };
            let (tx, rx) = tokio::sync::oneshot::channel();
            let relative = PathBuf::from(path);
            std::thread::spawn(move || {
                let _ = tx.send(files.collect(&handle, &[relative]));
            });
            let collected = rx.await.map_err(|_| "the collect thread died".to_owned())?;
            Ok(match (call, collected) {
                ("file_exists", Ok(_)) => Outcome::Document(serde_json::json!({
                    "path": path, "exists": true, "regular_file": true,
                })),
                ("read_file", Ok(artifacts)) => Outcome::Document(serde_json::json!({
                    "path": path, "bytes": artifacts.first().map_or(0, |a| a.bytes.len()),
                })),
                (_, Err(error)) => file_error(&error),
                (other, Ok(_)) => return Err(format!("no file call {other}")),
            })
        }
        fn launches(&self) -> u32 {
            std::fs::read_to_string(self.standin("docker.log"))
                .unwrap_or_default()
                .lines()
                .filter(|l| l.starts_with("exec -i ") && l.contains(&self.token))
                .count() as u32
        }
        fn descendant_alive(&self) -> Result<bool, Unmeasurable> {
            Ok(!live_processes(TOKEN, &self.token)?.is_empty())
        }
        /// Records the provider still resolves for the fixture's key, probed
        /// through public `status`: its operation ids are `c1-op<n>`, issued
        /// in sequence, so the first eight cover every start a hazard makes.
        async fn operation_records(&self) -> Option<u32> {
            let key = self.key.lock().unwrap().clone()?;
            let mut held = 0;
            for n in 1..=8 {
                let reference: OperationRef = serde_json::from_value(serde_json::json!({
                    "workspace": {"provider": "docker", "resource_id": "c1"},
                    "operation_id": format!("c1-op{n}"),
                    "operation_key": key,
                }))
                .unwrap();
                match self.provider.status(&reference).await {
                    Err(e) if e.kind == ErrorKind::UnknownOperation => {}
                    _ => held += 1,
                }
            }
            Some(held)
        }
        fn awaits_exit(&self, hazard: &str) -> bool {
            matches!(hazard, "LH-6" | "LH-7")
        }
    }

    /// What the Rust Docker provider does on each hazard, measured: a pass
    /// with the alternative each step matched, or the first miss's step, call
    /// and outcome fields. A change in any verdict fails here, so a fix or a
    /// regression has to update COVERAGE.md with it.
    fn expect(hazard: &str, verdict: &HazardVerdict) {
        let summary = match verdict {
            HazardVerdict::Pass { matched } => format!("pass {matched:?}"),
            HazardVerdict::Fail {
                step,
                call,
                mismatch,
            } => format!("fail {step} {call} {}", mismatch.outcome()),
            HazardVerdict::NotRun { step, call, .. } => format!("not run {step} {call}"),
        };
        let expected = match hazard {
            // Each step confirms: the cancel and the status are `cancelled`.
            "LH-1" => "pass [0, 0, 0]",
            // The retried cancel ends provider_unavailable after 7s with the
            // TERM-ignoring workload still alive. Not established whether the
            // stand-in's stop or the provider is at fault: COVERAGE.md.
            "LH-2" => "fail 3 cancel kind=provider_unavailable ref=true alive=true",
            // Finding: the cancel before the launch header ends
            // provider_unavailable after ~9s, the late workload alive. Measured
            // again with a stand-in that kills a held-back exec on `stop` and
            // refuses an exec into a stopped container: unchanged.
            "LH-3" => "fail 1 cancel kind=provider_unavailable ref=true alive=true",
            // Finding: start returns a ref and launches into the container
            // destroyed while it resolved, instead of resource_lost/cancelled.
            "LH-4" => "fail 0 start operation_key=\"exec-42:phase-1:fence-9:agent\" alive=false",
            // Finding: with the container stuck removing, the timed-out
            // operation's observe never completes within the 120s bound.
            "LH-5" => "fail 1 observe unsettled",
            // Finding: the gap page carries no loss_detected (0.2.1).
            "LH-6" => {
                "fail 2 observe state=\"succeeded\" complete=false gap={\"lost_bytes\":8} alive=false"
            }
            // Finding: `collect` opens the FIFO and blocks past the 10s bound.
            "LH-7" => "fail 2 file_exists unsettled",
            _ => unreachable!(),
        };
        assert_eq!(summary, expected, "{hazard}: {verdict:?}");
    }

    async fn run(hazard: &str) {
        let harness = StandIn::new(hazard);
        let verdict = drive_hazard(&fixture(hazard), &harness).await;
        expect(hazard, &verdict);
    }

    /// LH-5 through a decorator that records each provider call, so the
    /// fixture's "no status call before observe" is measured, not assumed.
    async fn lh5() {
        let harness = StandIn::with("LH-5", |inner| Records {
            inner,
            calls: std::sync::Mutex::new(Vec::new()),
        });
        let verdict = drive_hazard(&fixture("LH-5"), &harness).await;
        let calls = harness.provider.calls.lock().unwrap().clone();
        let observe = calls.iter().position(|c| *c == "observe");
        assert_eq!(calls.first(), Some(&"start"), "{calls:?}");
        assert!(observe.is_some(), "LH-5 never reached observe: {calls:?}");
        assert!(
            !calls[..observe.unwrap()].contains(&"status"),
            "status called before LH-5's first observe: {calls:?}"
        );
        expect("LH-5", &verdict);
    }

    // --- The per-case bound ---------------------------------------------------
    //
    // A Tokio timeout cannot interrupt a blocked thread, nor the synchronous
    // setup (`StandIn::new`), liveness measurement and cleanup (`Drop`) around
    // the futures it bounds. So each case runs in a process of its own, a
    // session of its own under `setsid -w`, and a plain thread here kills
    // that whole session once `CASE_BOUND` passes. One stuck case then fails
    // alone, within its bound, and the next still runs.

    /// Above the slowest case: LH-5's 120s observe bound plus setup.
    const CASE_BOUND: Duration = Duration::from_secs(180);
    /// Names the case `hazard_case` runs; unset, it runs nothing.
    const CASE: &str = "AW_HAZARD_CASE";
    /// Where the case writes once it has run to the end, so a filter that
    /// selects no test is never read as a pass.
    const CASE_DONE: &str = "AW_HAZARD_CASE_DONE";

    #[derive(Debug, PartialEq)]
    enum CaseEnd {
        Passed,
        /// The case process exited without completing: a failed assertion
        /// or a panic. Its output is in the test's.
        Failed(std::process::ExitStatus),
        /// Still running at the bound; its session was killed.
        OutOfBound(Duration),
    }

    /// Run `case` in its own bounded session and say how it ended.
    fn bounded(case: &str, bound: Duration) -> CaseEnd {
        let done = tempfile::tempdir().unwrap();
        let marker = done.path().join("done");
        let began = std::time::Instant::now();
        let mut child = std::process::Command::new("setsid")
            .arg("-w")
            .arg(std::env::current_exe().unwrap())
            .args(["--exact", "hazards::hazard_case", "--ignored"])
            .args(["--nocapture", "--test-threads=1", "-q"])
            .env(CASE, case)
            .env(CASE_DONE, &marker)
            .spawn()
            .expect("setsid(1) is required to bound a hazard case");
        // A spawned child never leads a process group, so setsid(1) does not
        // fork: the child is the session leader and its pid the session's
        // group id.
        let session = format!("-{}", child.id());
        let end = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break if status.success() && marker.exists() {
                    CaseEnd::Passed
                } else {
                    CaseEnd::Failed(status)
                };
            }
            if began.elapsed() >= bound {
                kill_session(&session);
                child.wait().unwrap();
                break CaseEnd::OutOfBound(began.elapsed());
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        // Whatever the case leaked into its session goes with it.
        kill_session(&session);
        end
    }

    fn kill_session(group: &str) {
        // ESRCH once the session is empty is the expected end.
        let _ = std::process::Command::new("kill")
            .args(["-KILL", "--", group])
            .stderr(std::process::Stdio::null())
            .status();
    }

    fn assert_passes(case: &str) {
        let end = bounded(case, CASE_BOUND);
        assert_eq!(end, CaseEnd::Passed, "{case} in its bounded session");
    }

    /// The entry point `bounded` runs in the case process. Ignored, so a
    /// plain `cargo test` never runs a case outside its bound.
    #[test]
    #[ignore = "one hazard case, run by `bounded` in its own session"]
    fn hazard_case() {
        let Ok(case) = std::env::var(CASE) else {
            return;
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        match case.as_str() {
            "LH-5" => runtime.block_on(lh5()),
            // The guard's own proof cases, below.
            "blocks-synchronously" => {
                let _harness = StandIn::new("LH-1");
                std::thread::sleep(Duration::from_secs(3600));
            }
            "fails" => panic!("the failing proof case"),
            "passes" => {}
            hazard => runtime.block_on(run(hazard)),
        }
        std::fs::write(std::env::var(CASE_DONE).unwrap(), "").unwrap();
    }

    /// The stand-in models a stopped container: an operation exec held back
    /// past a `stop` never runs its workload, so nothing is measured alive.
    #[tokio::test]
    async fn a_late_exec_into_a_stopped_container_is_refused() {
        let harness = StandIn::new("LH-3");
        let lh3 = fixture("LH-3");
        let spec = harness.spec("LH-3", &lh3.steps[0].args["spec"]);
        let started = harness.provider.start(&workspace(), spec).await;
        let stop = std::process::Command::new(harness.standin("docker"))
            .args(["stop", "c1"])
            .status()
            .unwrap();
        assert!(stop.success());
        tokio::time::sleep(Duration::from_secs(4)).await;
        assert!(
            !harness.descendant_alive().unwrap(),
            "the held-back workload ran after stop (start: {started:?})"
        );
    }

    #[test]
    fn lh1_double_fork_orphan_before_cancel() {
        assert_passes("LH-1");
    }
    #[test]
    fn lh2_interrupted_cancel() {
        assert_passes("LH-2");
    }
    #[test]
    fn lh3_cancel_before_launch_header() {
        assert_passes("LH-3");
    }
    #[test]
    fn lh4_destroy_during_start() {
        assert_passes("LH-4");
    }
    #[test]
    fn lh5_stuck_removal_settles() {
        assert_passes("LH-5");
    }
    #[test]
    fn lh6_drain_with_gap_reports_loss() {
        assert_passes("LH-6");
    }
    #[test]
    fn lh7_fifo_in_workspace() {
        assert_passes("LH-7");
    }

    /// The bound holds where Tokio's cannot: a case blocked synchronously in
    /// setup is killed, session and all, within it; a failing case fails; and
    /// the next case still runs and passes.
    #[test]
    fn a_synchronously_blocked_case_fails_within_its_bound_and_the_next_runs() {
        let bound = Duration::from_secs(5);
        match bounded("blocks-synchronously", bound) {
            CaseEnd::OutOfBound(elapsed) => {
                assert!(elapsed < bound + Duration::from_secs(2), "{elapsed:?}")
            }
            other => panic!("a blocked case ended {other:?}"),
        }
        assert!(matches!(bounded("fails", bound), CaseEnd::Failed(_)));
        assert_eq!(bounded("passes", bound), CaseEnd::Passed);
    }

    /// The record probe counts a started operation, and none before it, so
    /// LH-4's `operation_records: 0` is measured, not the probe's blindness.
    #[tokio::test]
    async fn operation_records_counts_a_started_operation() {
        let harness = StandIn::new("LH-1");
        let lh1 = fixture("LH-1");
        let spec = harness.spec("LH-1", &lh1.steps[0].args["spec"]);
        assert_eq!(harness.operation_records().await, Some(0));
        harness.provider.start(&workspace(), spec).await.unwrap();
        assert_eq!(harness.operation_records().await, Some(1));
    }

    /// Passes every call through, naming each in order.
    struct Records {
        inner: DockerOperations,
        calls: std::sync::Mutex<Vec<&'static str>>,
    }

    impl Records {
        fn note(&self, call: &'static str) {
            self.calls.lock().unwrap().push(call);
        }
    }

    impl SupportsOperations for Records {
        fn describe(&self) -> agentic_workspace_core::operations::ProviderDescriptor {
            self.inner.describe()
        }
        async fn start(
            &self,
            workspace: &WorkspaceRef,
            spec: OperationSpec,
        ) -> Result<OperationRef, OperationError> {
            self.note("start");
            self.inner.start(workspace, spec).await
        }
        async fn status(
            &self,
            operation: &OperationRef,
        ) -> Result<OperationStatus, OperationError> {
            self.note("status");
            self.inner.status(operation).await
        }
        async fn observe(
            &self,
            operation: &OperationRef,
            request: ObserveRequest,
        ) -> Result<agentic_workspace_core::operations::ObservationPage, OperationError> {
            self.note("observe");
            self.inner.observe(operation, request).await
        }
        async fn cancel(
            &self,
            operation: &OperationRef,
            grace: Duration,
        ) -> Result<OperationStatus, OperationError> {
            self.note("cancel");
            self.inner.cancel(operation, grace).await
        }
        async fn finalize(
            &self,
            workspace: &WorkspaceRef,
        ) -> agentic_workspace_core::operations::FinalizationOutcome {
            self.note("finalize");
            self.inner.finalize(workspace).await
        }
    }

    /// The mutation: a cancel that terminates nothing and reports confirmed.
    struct ConfirmsBlindly {
        inner: DockerOperations,
        applied: AtomicUsize,
    }

    impl SupportsOperations for ConfirmsBlindly {
        fn describe(&self) -> agentic_workspace_core::operations::ProviderDescriptor {
            self.inner.describe()
        }
        async fn start(
            &self,
            workspace: &WorkspaceRef,
            spec: OperationSpec,
        ) -> Result<OperationRef, OperationError> {
            self.inner.start(workspace, spec).await
        }
        async fn status(
            &self,
            operation: &OperationRef,
        ) -> Result<OperationStatus, OperationError> {
            self.inner.status(operation).await
        }
        async fn observe(
            &self,
            operation: &OperationRef,
            request: ObserveRequest,
        ) -> Result<agentic_workspace_core::operations::ObservationPage, OperationError> {
            self.inner.observe(operation, request).await
        }
        async fn cancel(
            &self,
            operation: &OperationRef,
            _grace: Duration,
        ) -> Result<OperationStatus, OperationError> {
            self.applied.fetch_add(1, Ordering::SeqCst);
            let mut status = serde_json::to_value(self.inner.status(operation).await?).unwrap();
            status["state"] = "cancelled".into();
            status["ended_at"] = "2026-01-01T00:00:00Z".into();
            Ok(serde_json::from_value(status).unwrap())
        }
        async fn finalize(
            &self,
            workspace: &WorkspaceRef,
        ) -> agentic_workspace_core::operations::FinalizationOutcome {
            self.inner.finalize(workspace).await
        }
    }

    /// No vacuous pass: a cancel confirmed while the measured descendant
    /// lives must fail LH-1 on the cancel step.
    #[tokio::test]
    async fn lh1_fails_a_cancel_confirmed_while_a_descendant_lives() {
        use agentic_workspace_conformance::hazard::{Mismatch, Outcome};
        let harness = StandIn::with("LH-1", |inner| ConfirmsBlindly {
            inner,
            applied: AtomicUsize::new(0),
        });
        let verdict = drive_hazard(&fixture("LH-1"), &harness).await;
        assert_eq!(
            harness.provider.applied.load(Ordering::SeqCst),
            1,
            "the mutation never ran"
        );
        let HazardVerdict::Fail {
            step: 1,
            call,
            mismatch: Mismatch::NoAlternative { measured },
        } = &verdict
        else {
            panic!("LH-1 did not fail on the confirmed cancel: {verdict:?}");
        };
        assert_eq!(call, "cancel");
        assert!(measured.descendant_alive, "{verdict:?}");
        assert!(
            matches!(&measured.outcome, Outcome::Document(d) if d["state"] == "cancelled"),
            "{verdict:?}"
        );
    }
}
