//! Docker implementation of the APSS 0.2.0 operation contract.
//!
//! Each operation is one `docker exec` whose wrapper starts the workload as
//! leader of a new session and prints its pid and start time on stdout's first
//! line, a channel the workload cannot write before it. Cancel signals that
//! group TERM then KILL, each only while the leader still has its recorded
//! start time, also signals by pid every descendant that left the group, and
//! confirms all of them gone. Anything it cannot confirm, or a pid it must
//! not signal, escalates to `docker stop`. The shell scripts are the Python
//! provider's, byte for byte, so both languages cancel the same way.

use agentic_workspace_core::operations::{
    EnforceableLimits, ErrorKind, FinalizationOutcome, FinalizationStage, FinalizationState,
    NonEmpty, ObservationDescriptor, ObservationIntegrity, ObservationPage, ObserveRequest,
    OperationError, OperationRef, OperationSpec, OperationSpool, OperationState, OperationStatus,
    ProviderDescriptor, SecurityProfile, StatusSchema, SupportsOperations, WorkspaceRef,
};
use std::collections::{BTreeMap, HashMap};
use std::num::NonZeroU64;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::Notify;

const OUTER: &str = r#"IFS= read -r argv0; IFS= read -r line; inner=$1; shift; setsid sh -c "$inner" "$argv0" "$line" "$@" & wait "$!""#;
const INNER: &str = r#"printf "%s %s\n" "$$" "$(cut -d" " -f22 /proc/$$/stat)"; [ -n "$1" ] && printf "%s\n" "$1"; shift; exec "$@""#;
const PROCESSES: &str = r#"for s in /proc/[0-9]*/stat; do st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; [ "$1" = Z ] && continue; pid=${s#/proc/}; echo "${pid%/stat} $2 $3 ${20}"; done"#;
const SIGNAL: &str = r#"start_of() { st=$(cat /proc/$1/stat 2>/dev/null) || return 0; set -- ${st##*) }; echo "${20}"; }; in_group() { for s in /proc/[0-9]*/stat; do st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; [ "$1" != Z ] && [ "$3" = "$group" ] && return 0; done; return 1; }; sig=$1; group=$2; group_start=$3; shift 3; failed=0; if [ -n "$group" ]; then cur=$(start_of "$group"); if [ -z "$cur" ] || [ "$cur" = "$group_start" ]; then if ! err=$(kill -s "$sig" -- "-$group" 2>&1) && in_group; then echo "group $group: $err" >&2; failed=1; fi; fi; fi; while [ $# -ge 2 ]; do cur=$(start_of "$1"); if [ "$cur" = "$2" ] && ! err=$(kill -s "$sig" "$1" 2>&1) && [ "$(start_of "$1")" = "$2" ]; then echo "pid $1: $err" >&2; failed=1; fi; shift 2; done; exit $failed"#;
const PID_NAMESPACE: &str = "readlink /proc/self/ns/pid";

/// How long the KILL round waits for the group to go.
const KILL_WAIT: Duration = Duration::from_secs(2);
/// How long output may keep arriving after the exec client exits: a
/// descendant holding the pipe (or a TERM handler's last words).
const FINAL_OUTPUT: Duration = Duration::from_secs(2);
/// How long the exec client may outlive a timed-out operation's termination
/// before it is detached, so a failed stop still settles.
const CLIENT_EXIT: Duration = Duration::from_secs(10);
/// The grace a timed-out operation gets between TERM and KILL.
const TIMEOUT_GRACE: Duration = Duration::from_secs(1);

/// pid -> (ppid, pgrp, start), as `PROCESSES` reports it.
type Table = HashMap<String, (String, String, String)>;

fn parse_table(out: &str) -> Table {
    out.lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            let numeric = fields.iter().all(|f| f.bytes().all(|b| b.is_ascii_digit()));
            (fields.len() == 4 && numeric).then(|| {
                let own = |i: usize| fields[i].to_owned();
                (own(0), (own(1), own(2), own(3)))
            })
        })
        .collect()
}

/// The leader's pid now names another process, so its group is empty.
fn leader_reused(table: &Table, leader: &str, start: &str) -> bool {
    table.get(leader).is_some_and(|row| row.2 != start)
}

/// Every live process of the operation: its group, then their descendants.
/// A descendant that left the group (`setsid`, `setpgid`) is still found
/// through its parent chain, and once tracked stays tracked by pid and start
/// time after its parent dies. Never pid 1, nor (unlike the Python
/// provider) init's other children through it.
fn members(
    table: &Table,
    leader: &str,
    start: &str,
    tracked: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    let reused = leader_reused(table, leader, start);
    let mut found: BTreeMap<String, String> = table
        .iter()
        .filter(|(pid, _)| pid.parse::<u64>().is_ok_and(|n| n > 1))
        .filter(|(pid, row)| (row.1 == leader && !reused) || tracked.get(*pid) == Some(&row.2))
        .map(|(pid, row)| (pid.clone(), row.2.clone()))
        .collect();
    loop {
        let children: Vec<(String, String)> = table
            .iter()
            .filter(|(pid, row)| !found.contains_key(*pid) && found.contains_key(&row.0))
            .map(|(pid, row)| (pid.clone(), row.2.clone()))
            .collect();
        if children.is_empty() {
            break;
        }
        found.extend(children);
    }
    found
}

/// Whether the group and every tracked process have gone from `table`.
fn all_gone(table: &Table, leader: &str, start: &str, tracked: &BTreeMap<String, String>) -> bool {
    let group = !leader_reused(table, leader, start) && table.values().any(|row| row.1 == leader);
    let escaped = tracked
        .iter()
        .any(|(pid, s)| table.get(pid).is_some_and(|row| &row.2 == s));
    !group && !escaped
}

/// Why `pid`'s group must not be signalled, or `None` when it may be. Init
/// and malformed pids are refused everywhere; our own group only when the
/// container shares our pid namespace, and an unreadable namespace counts as
/// shared, so doubt costs a container stop, never a stray kill.
fn unsafe_leader(
    pid: &str,
    own_group: Option<u64>,
    ours: Option<&str>,
    theirs: Option<&str>,
) -> Option<String> {
    let refusal = format!("refusing unsafe process group {pid:?}");
    let Some(number) = pid.parse::<u64>().ok().filter(|n| *n > 1) else {
        return Some(refusal);
    };
    if own_group.is_some_and(|own| own != number) {
        return None;
    }
    match (ours, theirs) {
        (Some(ours), Some(theirs)) if ours != theirs => None,
        _ => Some(format!("{refusal}: it may be the caller's own group")),
    }
}

/// Our process group, from `/proc/self/stat`; `None` when unreadable.
fn own_group() -> Option<u64> {
    let stat = std::fs::read_to_string("/proc/self/stat").ok()?;
    let rest = stat.rsplit_once(") ")?.1;
    rest.split_whitespace().nth(2)?.parse().ok()
}

/// A signal-killed workload exits 128+n through `wait`.
fn signal_name(code: i32) -> Option<String> {
    let name = match code.checked_sub(128)? {
        1 => "SIGHUP",
        2 => "SIGINT",
        3 => "SIGQUIT",
        6 => "SIGABRT",
        9 => "SIGKILL",
        13 => "SIGPIPE",
        14 => "SIGALRM",
        15 => "SIGTERM",
        _ => return None,
    };
    Some(name.to_owned())
}

fn now_rfc3339() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let days = (secs / 86_400) as i64;
    let (hh, mm, ss) = (secs % 86_400 / 3_600, secs % 3_600 / 60, secs % 60);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

fn error(
    kind: ErrorKind,
    message: impl Into<String>,
    operation: Option<&OperationRef>,
) -> OperationError {
    OperationError::new(kind, message, operation.cloned())
}

/// What is known of one operation; guarded by its record's mutex.
struct Progress {
    state: OperationState,
    spool: OperationSpool,
    /// The trusted `(pid, start)` of the session leader, once printed.
    leader: Option<(String, String)>,
    /// Set once stdout's first line has been read or cannot come.
    leader_known: bool,
    /// The outcome a cancel or timeout asked for, recorded before signalling.
    requested: Option<OperationState>,
    tracked: BTreeMap<String, String>,
    exit_code: Option<i64>,
    signal: Option<String>,
    ended_at: Option<String>,
    escalated: bool,
    failures: Vec<String>,
}

struct Operation {
    reference: OperationRef,
    spec: OperationSpec,
    container: String,
    started_at: String,
    progress: Mutex<Progress>,
    changed: Notify,
}

impl Operation {
    fn lock(&self) -> MutexGuard<'_, Progress> {
        self.progress.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn update(&self, f: impl FnOnce(&mut Progress)) {
        f(&mut self.lock());
        self.changed.notify_waiters();
    }

    /// Wait until `ready` holds or `within` passes; whether it held.
    async fn wait_for(&self, within: Duration, ready: impl Fn(&Progress) -> bool) -> bool {
        let deadline = tokio::time::Instant::now() + within;
        loop {
            let notified = self.changed.notified();
            if ready(&self.lock()) {
                return true;
            }
            if tokio::time::timeout_at(deadline, notified).await.is_err() {
                return ready(&self.lock());
            }
        }
    }

    fn status(&self) -> OperationStatus {
        let p = self.lock();
        let terminal = p.state.is_terminal();
        let mut metadata = self.spec.metadata.get().clone();
        if p.escalated {
            metadata.insert("escalation".into(), "container_stopped".into());
        }
        if !p.failures.is_empty() {
            metadata.insert("signal_failure".into(), p.failures.join("; "));
        }
        OperationStatus {
            schema: Some(StatusSchema::V1),
            operation: self.reference.clone(),
            state: p.state,
            exit_code: terminal.then_some(p.exit_code),
            signal: terminal.then(|| p.signal.clone()),
            started_at: Some(self.started_at.clone()),
            ended_at: if terminal { p.ended_at.clone() } else { None },
            execution_id: self.spec.execution_id.clone(),
            metadata: metadata
                .try_into()
                .unwrap_or_else(|_| self.spec.metadata.clone()),
        }
    }
}

/// The `docker` client every operation and cancel runs through.
#[derive(Clone)]
struct Docker(Arc<PathBuf>);

impl Docker {
    async fn run(&self, args: &[&str]) -> Result<(i32, String), String> {
        let output = Command::new(self.0.as_path())
            .args(args)
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output()
            .await
            .map_err(|e| format!("{}: {e}", self.0.display()))?;
        let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
        text.push_str(&String::from_utf8_lossy(&output.stderr));
        Ok((output.status.code().unwrap_or(-1), text))
    }

    async fn exec(
        &self,
        container: &str,
        script: &str,
        name: &str,
        args: &[&str],
    ) -> Result<String, String> {
        let mut argv = vec!["exec", container, "sh", "-c", script, name];
        argv.extend_from_slice(args);
        match self.run(&argv).await? {
            (0, out) => Ok(out),
            (code, out) => Err(format!("{name}: exit {code}: {}", out.trim())),
        }
    }

    async fn table(&self, container: &str) -> Result<Table, String> {
        Ok(parse_table(
            &self.exec(container, PROCESSES, "aw-processes", &[]).await?,
        ))
    }

    /// Cancel `op` once and wait for it to settle. Idempotent: a terminal or
    /// already-ending operation is only waited for.
    async fn cancel(&self, op: &Operation, grace: Duration) -> Result<(), OperationError> {
        let first = {
            let mut p = op.lock();
            let first = !p.state.is_terminal() && p.requested.is_none();
            if first {
                p.requested = Some(OperationState::Cancelled);
            }
            first
        };
        if first {
            self.terminate(op, grace).await?;
        }
        // `collect` settles once the client exits; a stopped container ends it.
        op.wait_for(grace + KILL_WAIT + FINAL_OUTPUT * 2, |p| {
            p.state.is_terminal()
        })
        .await;
        Ok(())
    }

    /// TERM the operation, KILL after `grace`, confirm; else stop the
    /// container. A stop that cannot run or fails is `provider_unavailable`:
    /// the operation may still be running.
    async fn terminate(&self, op: &Operation, grace: Duration) -> Result<(), OperationError> {
        op.wait_for(KILL_WAIT, |p| p.leader_known).await;
        let leader = op.lock().leader.clone();
        if let Some((pid, start)) = leader {
            // Only a leader numbered like our own group needs the namespace probe.
            let own = own_group();
            let shared = pid
                .parse::<u64>()
                .is_ok_and(|n| n > 1 && own.is_none_or(|own| own == n));
            let ours = std::fs::read_link("/proc/self/ns/pid")
                .ok()
                .map(|p| p.to_string_lossy().into_owned());
            let theirs = match shared {
                true => self
                    .exec(&op.container, PID_NAMESPACE, "aw-pidns", &[])
                    .await
                    .ok(),
                false => None,
            };
            let theirs = theirs
                .map(|t| t.trim().to_owned())
                .filter(|t| !t.is_empty());
            let refusal = unsafe_leader(&pid, own, ours.as_deref(), theirs.as_deref());
            match refusal {
                Some(refusal) => op.update(|p| p.failures.push(refusal)),
                None => match self.signal_group(op, &pid, &start, grace).await {
                    Ok(true) => return Ok(()),
                    Ok(false) => {}
                    Err(why) => op.update(|p| p.failures.push(format!("signalling: {why}"))),
                },
            }
        }
        op.update(|p| p.escalated = true);
        match self.run(&["stop", "-t", "0", &op.container]).await {
            Ok((0, _)) => Ok(()),
            Ok((code, out)) => Err(format!(
                "docker stop {}: exit {code}: {}",
                op.container,
                out.trim()
            )),
            Err(why) => Err(why),
        }
        .map_err(|why| {
            op.update(|p| p.failures.push(why.clone()));
            error(ErrorKind::ProviderUnavailable, why, Some(&op.reference))
        })
    }

    /// TERM, then KILL; whether every process of the operation is confirmed gone.
    async fn signal_group(
        &self,
        op: &Operation,
        pid: &str,
        start: &str,
        grace: Duration,
    ) -> Result<bool, String> {
        for (signal, wait) in [("TERM", grace), ("KILL", KILL_WAIT)] {
            let table = self.table(&op.container).await?;
            let tracked = {
                let mut p = op.lock();
                let found = members(&table, pid, start, &p.tracked);
                p.tracked.extend(found);
                p.tracked.clone()
            };
            let group = if leader_reused(&table, pid, start) {
                ""
            } else {
                pid
            };
            let mut args = vec![signal, group, start];
            args.extend(tracked.iter().flat_map(|(p, s)| [p.as_str(), s.as_str()]));
            self.exec(&op.container, SIGNAL, "aw-signal", &args).await?;
            let deadline = tokio::time::Instant::now() + wait;
            loop {
                if let Ok(table) = self.table(&op.container).await {
                    if all_gone(&table, pid, start, &op.lock().tracked) {
                        return Ok(true);
                    }
                }
                if tokio::time::Instant::now() >= deadline {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
        Ok(false)
    }
}

/// Runs operations in Docker workspaces. A [`WorkspaceRef`]'s `resource_id`
/// is the container; nothing assumes a host path.
pub struct DockerOperations {
    docker: Docker,
    retention: NonZeroU64,
    max_page: NonZeroU64,
    sequence: AtomicU64,
    /// Keyed by (container, operation_key). The async lock spans the launch,
    /// so racing same-key starts see the first record and launch nothing.
    records: tokio::sync::Mutex<HashMap<(String, String), Arc<Operation>>>,
}

impl DockerOperations {
    pub fn new(
        docker: impl Into<PathBuf>,
        retention_bytes: NonZeroU64,
        max_page_bytes: NonZeroU64,
    ) -> Self {
        Self {
            docker: Docker(Arc::new(docker.into())),
            retention: retention_bytes,
            max_page: max_page_bytes,
            sequence: AtomicU64::new(0),
            records: tokio::sync::Mutex::new(HashMap::new()),
        }
    }

    async fn lookup(&self, reference: &OperationRef) -> Result<Arc<Operation>, OperationError> {
        let key = (
            reference.workspace.resource_id.to_string(),
            reference.operation_key.to_string(),
        );
        self.records
            .lock()
            .await
            .get(&key)
            .filter(|op| op.reference == *reference)
            .cloned()
            .ok_or_else(|| {
                error(
                    ErrorKind::UnknownOperation,
                    format!(
                        "operation {} is not known to this provider",
                        reference.operation_id
                    ),
                    Some(reference),
                )
            })
    }

    fn client_argv(container: &str, spec: &OperationSpec) -> Vec<String> {
        let workdir = format!(
            "/workspace/{}",
            spec.working_directory.as_str().trim_end_matches('/')
        );
        let mut argv: Vec<String> = ["exec", "-i", "-w", &workdir].map(String::from).into();
        for (name, value) in spec.environment.get() {
            argv.extend(["-e".to_owned(), format!("{name}={value}")]);
        }
        argv.extend([container, "sh", "-c", OUTER, "aw-operation", INNER].map(String::from));
        argv.extend(spec.argv.as_slice().iter().cloned());
        argv
    }

    async fn launch(
        &self,
        container: &str,
        spec: OperationSpec,
    ) -> Result<Arc<Operation>, OperationError> {
        if let Some(binding) = spec.credential_bindings.first() {
            return Err(error(
                ErrorKind::UnsupportedCapability,
                format!(
                    "credential binding {:?}: the Rust docker provider has no credential resolver yet",
                    binding.target
                ),
                None,
            ));
        }
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        let reference = OperationRef {
            workspace: WorkspaceRef {
                schema: None,
                provider: NonEmpty::try_from("docker").expect("non-empty"),
                resource_id: NonEmpty::try_from(container).expect("checked by caller"),
            },
            operation_id: NonEmpty::try_from(format!("{container}-op{sequence}"))
                .expect("non-empty"),
            operation_key: spec.operation_key.clone(),
        };
        let mut child = Command::new(self.docker.0.as_path())
            .args(Self::client_argv(container, &spec))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| {
                error(
                    ErrorKind::ProviderUnavailable,
                    format!("{}: {e}", self.docker.0.display()),
                    Some(&reference),
                )
            })?;
        let (argv0, line) = spec
            .launch_marker
            .as_ref()
            .map_or(("sh", ""), |m| (m.argv0.as_str(), m.line.as_str()));
        let preamble = format!("{argv0}\n{line}\n");
        let mut stdin = child.stdin.take().expect("piped");
        let operation = Arc::new(Operation {
            reference,
            container: container.to_owned(),
            started_at: now_rfc3339(),
            progress: Mutex::new(Progress {
                state: OperationState::Running,
                spool: OperationSpool::new(self.retention),
                leader: None,
                leader_known: false,
                requested: None,
                tracked: BTreeMap::new(),
                exit_code: None,
                signal: None,
                ended_at: None,
                escalated: false,
                failures: Vec::new(),
            }),
            changed: Notify::new(),
            spec,
        });
        // A failed write means the client already died; `collect` settles it.
        let _ = stdin.write_all(preamble.as_bytes()).await;
        drop(stdin);
        tokio::spawn(collect(self.docker.clone(), operation.clone(), child));
        Ok(operation)
    }
}

/// Pump output, enforce the timeout, and settle once the client exits.
async fn collect(docker: Docker, op: Arc<Operation>, mut child: tokio::process::Child) {
    let stdout = child.stdout.take().expect("piped");
    let stderr = child.stderr.take().expect("piped");
    let readers = tokio::spawn({
        let op = op.clone();
        async move { tokio::join!(read_stdout(&op, stdout), pump(&op, stderr)) }
    });
    let timeout = Duration::from_secs(op.spec.timeout_seconds.get());
    let code = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(status) => status.ok().and_then(|s| s.code()),
        Err(_) => {
            let first = {
                let mut p = op.lock();
                p.requested.get_or_insert(OperationState::TimedOut) == &OperationState::TimedOut
            };
            if first {
                if let Err(e) = docker.terminate(&op, TIMEOUT_GRACE).await {
                    op.update(|p| p.failures.push(e.message.to_string()));
                }
            }
            match tokio::time::timeout(CLIENT_EXIT, child.wait()).await {
                Ok(status) => status.ok().and_then(|s| s.code()),
                Err(_) => {
                    // The workload outlived the stop. Detach from it, and say so.
                    let _ = child.kill().await;
                    op.update(|p| {
                        p.failures.push(format!(
                            "exec client still attached {}s after the timeout's termination; \
                             detached, the workload may still be running",
                            CLIENT_EXIT.as_secs()
                        ))
                    });
                    None
                }
            }
        }
    };
    if tokio::time::timeout(FINAL_OUTPUT, readers).await.is_err() {
        op.update(|p| {
            p.failures
                .push("output still open after the client exited; detached".into())
        });
    }
    op.update(|p| {
        p.leader_known = true;
        p.ended_at = Some(now_rfc3339());
        p.state = match p.requested {
            Some(requested) => requested,
            None => {
                p.exit_code = code.map(i64::from);
                p.signal = code.and_then(signal_name);
                if code == Some(0) {
                    OperationState::Succeeded
                } else {
                    OperationState::Failed
                }
            }
        };
    });
}

async fn read_stdout(op: &Operation, stdout: impl AsyncRead + Unpin) {
    let mut reader = BufReader::new(stdout);
    let mut header = Vec::new();
    let _ = reader.read_until(b'\n', &mut header).await;
    let text = String::from_utf8_lossy(&header);
    let fields: Vec<&str> = text.split_whitespace().collect();
    op.update(|p| {
        if fields.len() == 2 && fields.iter().all(|f| f.bytes().all(|b| b.is_ascii_digit())) {
            p.leader = Some((fields[0].to_owned(), fields[1].to_owned()));
        } else {
            // Not the wrapper's line (it failed before exec); keep it.
            p.spool.append(&header);
        }
        p.leader_known = true;
    });
    pump(op, reader).await;
}

async fn pump(op: &Operation, mut stream: impl AsyncRead + Unpin) {
    let mut buffer = vec![0; 64 * 1024];
    while let Ok(n @ 1..) = stream.read(&mut buffer).await {
        op.update(|p| p.spool.append(&buffer[..n]));
    }
}

impl SupportsOperations for DockerOperations {
    fn describe(&self) -> ProviderDescriptor {
        let names = |items: &[&str]| {
            items
                .iter()
                .map(|n| NonEmpty::try_from(*n).expect("non-empty"))
                .collect::<Vec<_>>()
                .try_into()
                .expect("unique")
        };
        ProviderDescriptor {
            schema: None,
            provider: NonEmpty::try_from("docker").expect("non-empty"),
            security_profile: SecurityProfile::Isolated,
            enforceable_limits: EnforceableLimits {
                cpu: true,
                memory: true,
                disk: false,
                network: true,
                timeout: true,
            },
            capabilities: names(&["operations", "cancel", "launch_marker"]),
            observation: ObservationDescriptor {
                retention_bytes: self.retention,
                max_page_bytes: self.max_page,
                integrity: ObservationIntegrity::Exact,
            },
            interactive: false,
            environment_kinds: names(&["oci-image"]),
        }
    }

    async fn start(
        &self,
        workspace: &WorkspaceRef,
        spec: OperationSpec,
    ) -> Result<OperationRef, OperationError> {
        if workspace.provider.as_str() != "docker" {
            return Err(error(
                ErrorKind::UnknownOperation,
                format!(
                    "workspace {:?} was not issued by docker",
                    workspace.provider.as_str()
                ),
                None,
            ));
        }
        let key = (
            workspace.resource_id.to_string(),
            spec.operation_key.to_string(),
        );
        let mut records = self.records.lock().await;
        if let Some(existing) = records.get(&key) {
            if existing.spec != spec {
                return Err(error(
                    ErrorKind::Conflict,
                    format!(
                        "operation_key {:?} already started with a different spec",
                        key.1
                    ),
                    Some(&existing.reference),
                ));
            }
            return Ok(existing.reference.clone());
        }
        let operation = self.launch(&key.0, spec).await?;
        let reference = operation.reference.clone();
        records.insert(key, operation);
        Ok(reference)
    }

    async fn status(&self, operation: &OperationRef) -> Result<OperationStatus, OperationError> {
        Ok(self.lookup(operation).await?.status())
    }

    async fn observe(
        &self,
        operation: &OperationRef,
        request: ObserveRequest,
    ) -> Result<ObservationPage, OperationError> {
        let op = self.lookup(operation).await?;
        let cursor = request.cursor.as_deref();
        let limit = usize::try_from(self.max_page.get()).unwrap_or(usize::MAX);
        let max_bytes = if request.max_bytes == 0 {
            limit
        } else {
            request.max_bytes.min(limit)
        };
        let page = |p: &Progress| p.spool.page(&op.reference, p.state, cursor, max_bytes);
        let first = page(&op.lock())?;
        if !first.data_b64.as_str().is_empty() || first.complete || request.wait.is_zero() {
            return Ok(first);
        }
        let end = op.lock().spool.end();
        op.wait_for(request.wait, |p| {
            p.spool.end() != end || p.state.is_terminal()
        })
        .await;
        page(&op.lock())
    }

    async fn cancel(
        &self,
        operation: &OperationRef,
        grace: Duration,
    ) -> Result<OperationStatus, OperationError> {
        let op = self.lookup(operation).await?;
        self.docker.cancel(&op, grace).await?;
        Ok(op.status())
    }

    async fn finalize(&self, workspace: &WorkspaceRef) -> FinalizationOutcome {
        let mine: Vec<Arc<Operation>> = {
            let mut records = self.records.lock().await;
            let ours = |container: &String| container == workspace.resource_id.as_str();
            let mine = records
                .iter()
                .filter(|((c, _), _)| ours(c))
                .map(|(_, op)| op.clone())
                .collect();
            records.retain(|(container, _), _| !ours(container));
            mine
        };
        let mut failures = Vec::new();
        for op in mine {
            if let Err(e) = self.docker.cancel(&op, TIMEOUT_GRACE).await {
                failures.push(e.message.to_string());
            } else if !op.lock().state.is_terminal() {
                failures.push(format!(
                    "operation {} did not end",
                    op.reference.operation_id
                ));
            }
        }
        match failures.is_empty() {
            true => FinalizationOutcome {
                schema: None,
                state: FinalizationState::Completed,
                stage: None,
                error: None,
                retained: None,
            },
            false => FinalizationOutcome {
                schema: None,
                state: FinalizationState::Failed,
                stage: Some(FinalizationStage::WhileRunning),
                error: Some(error(
                    ErrorKind::ProviderUnavailable,
                    failures.join("; "),
                    None,
                )),
                retained: None,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(rows: &[&str]) -> Table {
        parse_table(&rows.join("\n"))
    }

    #[test]
    fn the_process_table_skips_malformed_rows() {
        let parsed = table(&["10 1 10 500", "garbage", "11 10 10", "12 x 10 501"]);
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed["10"], ("1".into(), "10".into(), "500".into()));
    }

    #[test]
    fn members_follow_escapees_and_never_include_init() {
        // 10 leads; 11 is in its group; 12 left the group via setsid but is
        // 11's child; 13 is 12's child; 1 claims group 10 and is never a member.
        let rows = table(&[
            "10 1 10 500",
            "11 10 10 501",
            "12 11 12 502",
            "13 12 12 503",
            "1 0 10 1",
            "20 1 20 600",
        ]);
        let found = members(&rows, "10", "500", &BTreeMap::new());
        let pids: Vec<&str> = found.keys().map(String::as_str).collect();
        assert_eq!(pids, ["10", "11", "12", "13"]);
        assert!(!all_gone(&rows, "10", "500", &found));
    }

    #[test]
    fn a_tracked_escapee_whose_parent_died_is_still_a_member() {
        let tracked = BTreeMap::from([("12".to_owned(), "502".to_owned())]);
        let rows = table(&["12 1 12 502"]);
        assert!(members(&rows, "10", "500", &tracked).contains_key("12"));
        assert!(!all_gone(&rows, "10", "500", &tracked));
        // Same pid, new start time: a reused pid is not ours.
        let reused = table(&["12 1 12 999"]);
        assert!(members(&reused, "10", "500", &tracked).is_empty());
        assert!(all_gone(&reused, "10", "500", &tracked));
    }

    #[test]
    fn the_group_of_a_reused_leader_is_never_ours() {
        let rows = table(&["10 1 10 777", "11 10 10 501"]);
        assert!(leader_reused(&rows, "10", "500"));
        assert!(members(&rows, "10", "500", &BTreeMap::new()).is_empty());
        assert!(all_gone(&rows, "10", "500", &BTreeMap::new()));
    }

    #[test]
    fn init_malformed_and_our_own_group_are_refused() {
        for pid in ["0", "1", "-5", "", "x"] {
            assert!(
                unsafe_leader(pid, Some(42), Some("a"), Some("b")).is_some(),
                "{pid:?}"
            );
        }
        assert!(unsafe_leader("7", Some(42), None, None).is_none());
        // Our own group number: safe only in a provably different namespace.
        assert!(unsafe_leader("42", Some(42), Some("pid:[1]"), Some("pid:[2]")).is_none());
        assert!(unsafe_leader("42", Some(42), Some("pid:[1]"), Some("pid:[1]")).is_some());
        assert!(unsafe_leader("42", Some(42), Some("pid:[1]"), None).is_some());
        assert!(unsafe_leader("42", None, None, Some("pid:[2]")).is_some());
    }

    #[test]
    fn signalled_exits_are_named() {
        assert_eq!(signal_name(143).as_deref(), Some("SIGTERM"));
        assert_eq!(signal_name(137).as_deref(), Some("SIGKILL"));
        assert_eq!(signal_name(1), None);
    }

    #[test]
    fn timestamps_are_rfc3339_utc() {
        let now = now_rfc3339();
        assert_eq!(now.len(), 20, "{now}");
        assert!(now.ends_with('Z') && now.as_bytes()[10] == b'T', "{now}");
    }
}
