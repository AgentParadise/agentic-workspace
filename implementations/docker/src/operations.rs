//! Docker implementation of the APSS 0.2.0 operation contract.
//!
//! Each operation is one `docker exec` whose wrapper starts the workload as
//! leader of a new session and prints its pid and start time on stdout's first
//! line, a channel the workload cannot write before it. Cancel signals that
//! group TERM then KILL, each only while the leader still has its recorded
//! start time, also signals by pid every descendant that left the group, and
//! confirms all of them gone, and no untracked orphan of init started since
//! the wrapper survives. Anything it cannot confirm, or a pid it must not
//! signal, escalates to `docker stop`, itself confirmed by the container's
//! state before the cancel is. The shell scripts are the Python
//! provider's, byte for byte, so both languages cancel the same way.

use agentic_workspace_core::operations::{
    CredentialDelivery, CredentialResolver, EnforceableLimits, ErrorKind, FinalizationOutcome,
    FinalizationStage, FinalizationState, NonEmpty, ObservationDescriptor, ObservationIntegrity,
    ObservationPage, ObserveRequest, OperationError, OperationRef, OperationSpec, OperationSpool,
    OperationState, OperationStatus, ProviderDescriptor, SecurityProfile, StatusSchema,
    SupportsOperations, WorkspaceRef,
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
/// How long settlement waits out a container removal in progress.
const REMOVAL_WAIT: Duration = Duration::from_secs(30);

/// What `docker container inspect` says of an operation's container.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Presence {
    Running,
    Removing,
    Stopped,
    Gone,
}

/// Classify `docker container inspect -f {{.State.Status}}`; an answer that
/// is neither a state nor "no such container" means docker cannot tell.
fn presence(code: i32, out: &str) -> Result<Presence, String> {
    match (code, out.trim()) {
        (0, "running" | "paused" | "restarting") => Ok(Presence::Running),
        (0, "removing") => Ok(Presence::Removing),
        (0, _) => Ok(Presence::Stopped),
        (_, out) if out.to_lowercase().contains("no such") => Ok(Presence::Gone),
        (code, out) => Err(format!(
            "docker could not report the container: exit {code}: {out}"
        )),
    }
}

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

/// Orphans of the container's init that started no earlier than the
/// operation's wrapper and are not tracked: a descendant that left the group
/// and lost its parent before cancel first looked has no parent chain back to
/// the operation, so nothing proves it is not ours. One started before the
/// wrapper cannot be.
fn unattributed(table: &Table, start: &str, tracked: &BTreeMap<String, String>) -> Vec<String> {
    let Ok(start) = start.parse::<u64>() else {
        return table.keys().cloned().collect();
    };
    let mut orphans: Vec<String> = table
        .iter()
        .filter(|(pid, row)| {
            row.0 == "1"
                && row.2.parse::<u64>().is_ok_and(|s| s >= start)
                && tracked.get(*pid) != Some(&row.2)
        })
        .map(|(pid, _)| pid.clone())
        .collect();
    orphans.sort_by_key(|pid| pid.parse::<u64>().unwrap_or(u64::MAX));
    orphans
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

/// Which of the exec client's pipes a chunk came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Channel {
    Stdout = 0,
    Stderr = 1,
}

/// What is known of one operation; guarded by its record's mutex.
struct Progress {
    state: OperationState,
    /// Both channels, each chunk in the order its reader received it. The
    /// two pipes are read independently, so nothing orders one channel's
    /// bytes against the other's.
    spool: OperationSpool,
    /// Per-channel copies (stdout, stderr), each in its own order, kept only
    /// when the provider was built `with_separate_channels`.
    channels: Option<[Vec<u8>; 2]>,
    /// The trusted `(pid, start)` of the session leader, once printed.
    leader: Option<(String, String)>,
    /// Set once stdout's first line has been read or cannot come.
    leader_known: bool,
    /// The outcome a cancel or timeout asked for, recorded before signalling.
    requested: Option<OperationState>,
    /// A termination is in flight; cleared when one fails, so it is retried.
    terminating: bool,
    /// The cancel's termination confirmed every process gone or the container
    /// stopped. Only this lets settlement publish `cancelled`.
    cancel_confirmed: bool,
    tracked: BTreeMap<String, String>,
    exit_code: Option<i64>,
    signal: Option<String>,
    ended_at: Option<String>,
    escalated: bool,
    failures: Vec<String>,
    /// Why output stopped being collected before its streams ended cleanly.
    output_failures: Vec<String>,
    /// The exec client has exited and its output is drained; settlement may
    /// still wait on the container's presence.
    client_finished: bool,
    client_code: Option<i32>,
}

struct Operation {
    reference: OperationRef,
    spec: OperationSpec,
    container: String,
    started_at: String,
    progress: Mutex<Progress>,
    changed: Notify,
}

impl Progress {
    /// Store `chunk`, copying to `channels` what the spool stored (redacted),
    /// never the raw chunk.
    fn record(&mut self, chunk: &[u8], channel: Channel) {
        let stored = self.spool.append_channel(chunk, channel as usize);
        self.keep(&stored, channel as usize);
    }

    /// Store every tail the spool held back: the readers have reached EOF.
    fn finish_output(&mut self) {
        for (channel, stored) in self.spool.finish() {
            self.keep(&stored, channel);
        }
    }

    fn keep(&mut self, stored: &[u8], channel: usize) {
        if let Some(channels) = &mut self.channels {
            channels[channel].extend_from_slice(stored);
        }
    }
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
        if !p.output_failures.is_empty() {
            metadata.insert("output_failure".into(), p.output_failures.join("; "));
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

    async fn presence(&self, container: &str) -> Result<Presence, String> {
        let (code, out) = self
            .run(&["container", "inspect", "-f", "{{.State.Status}}", container])
            .await?;
        presence(code, &out).map_err(|why| format!("{container}: {why}"))
    }

    /// Settle a finished client's operation. A workload whose container is
    /// gone is `lost`, not `failed`; while docker cannot tell, or the
    /// container is still being removed, it stays unsettled and `status`
    /// probes again. `wait` waits out a removal in progress.
    async fn settle(&self, op: &Operation, wait: bool) {
        let deadline = tokio::time::Instant::now() + REMOVAL_WAIT;
        let presence = loop {
            match self.presence(&op.container).await {
                Ok(Presence::Removing) if wait && tokio::time::Instant::now() < deadline => {
                    tokio::time::sleep(Duration::from_millis(100)).await
                }
                other => break other,
            }
        };
        let Ok(presence) = presence else { return };
        if presence == Presence::Removing {
            return;
        }
        op.update(|p| {
            if p.state.is_terminal() {
                return;
            }
            p.leader_known = true;
            p.ended_at = Some(now_rfc3339());
            p.state = match (p.requested, presence) {
                (Some(OperationState::Cancelled), _) if !p.cancel_confirmed => {
                    if p.terminating {
                        // The cancel is still confirming; it settles once it has.
                        p.ended_at = None;
                        return;
                    }
                    // The cancel could not confirm the operation ended: say so.
                    p.exit_code = p.client_code.map(i64::from);
                    OperationState::Failed
                }
                (Some(requested), _) => requested,
                (None, Presence::Gone) => OperationState::Lost,
                (None, _) => {
                    let code = p.client_code;
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

    async fn table(&self, container: &str) -> Result<Table, String> {
        Ok(parse_table(
            &self.exec(container, PROCESSES, "aw-processes", &[]).await?,
        ))
    }

    /// Cancel `op` and wait for it to settle. Idempotent: a terminal or
    /// already-ending operation is only waited for. A failed termination is
    /// retried by the next cancel, and an operation that has not ended is an
    /// error, never a successful cancel.
    async fn cancel(&self, op: &Operation, grace: Duration) -> Result<(), OperationError> {
        let first = {
            let mut p = op.lock();
            let first = !p.state.is_terminal() && !p.terminating;
            if first {
                p.terminating = true;
                p.requested.get_or_insert(OperationState::Cancelled);
            }
            first
        };
        if first {
            if let Err(e) = self.terminate(op, grace).await {
                op.update(|p| p.terminating = false);
                return Err(e);
            }
            let finished = {
                let mut p = op.lock();
                p.cancel_confirmed = true;
                p.client_finished
            };
            op.changed.notify_waiters();
            // The collector may have finished while this cancel was confirming.
            if finished {
                self.settle(op, true).await;
            }
        }
        // `collect` settles once the client exits; a stopped container ends it.
        let wait = grace + KILL_WAIT + FINAL_OUTPUT * 2;
        match op.wait_for(wait, |p| p.state.is_terminal()).await {
            true => Ok(()),
            false => Err(error(
                ErrorKind::ProviderUnavailable,
                format!(
                    "operation {} did not end within {}s of its cancellation",
                    op.reference.operation_id,
                    wait.as_secs()
                ),
                Some(&op.reference),
            )),
        }
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
        self.stop_confirmed(&op.container).await.map_err(|why| {
            op.update(|p| p.failures.push(why.clone()));
            error(ErrorKind::ProviderUnavailable, why, Some(&op.reference))
        })
    }

    /// `docker stop` the container and observe it stopped or gone. A stop
    /// that reports success is confirmed by the daemon's own state: running,
    /// an unreadable state, or a removal still in progress after
    /// `REMOVAL_WAIT` leaves the termination unconfirmed.
    async fn stop_confirmed(&self, container: &str) -> Result<(), String> {
        match self.run(&["stop", "-t", "0", container]).await? {
            (0, _) => {}
            (code, out) => {
                return Err(format!(
                    "docker stop {container}: exit {code}: {}",
                    out.trim()
                ));
            }
        }
        let deadline = tokio::time::Instant::now() + REMOVAL_WAIT;
        loop {
            match self.presence(container).await? {
                Presence::Stopped | Presence::Gone => return Ok(()),
                Presence::Running => {
                    return Err(format!(
                        "docker stop {container} reported success but the container is still running"
                    ));
                }
                Presence::Removing if tokio::time::Instant::now() >= deadline => {
                    return Err(format!(
                        "docker stop {container} reported success but the container is still \
                         being removed {}s later; its termination is unconfirmed",
                        REMOVAL_WAIT.as_secs()
                    ));
                }
                Presence::Removing => tokio::time::sleep(Duration::from_millis(100)).await,
            }
        }
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
                    let tracked = op.lock().tracked.clone();
                    if all_gone(&table, pid, start, &tracked) {
                        // Ownership of an orphan adopted by init cannot be
                        // proven, so any such survivor fails closed into a
                        // container stop, which also ends unrelated detached
                        // processes of the workspace: the price of a
                        // confirmed cancel.
                        let orphans = unattributed(&table, start, &tracked);
                        if orphans.is_empty() {
                            return Ok(true);
                        }
                        op.update(|p| {
                            p.failures.push(format!(
                                "processes {orphans:?} were orphaned to init after the \
                                 operation began and may be its detached descendants; \
                                 stopping the container"
                            ))
                        });
                        return Ok(false);
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

/// (container, operation_key).
type Key = (String, String);

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// One key's launch lock, shared by every start of that key in flight and
/// dropped from the map with the last of them.
struct KeySlot<'a> {
    slots: &'a Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>,
    key: Key,
    lock: Arc<tokio::sync::Mutex<()>>,
}

impl<'a> KeySlot<'a> {
    fn claim(slots: &'a Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>, key: Key) -> Self {
        let lock = guard(slots).entry(key.clone()).or_default().clone();
        Self { slots, key, lock }
    }
}

impl Drop for KeySlot<'_> {
    fn drop(&mut self) {
        // Every clone is taken under the map's lock, so two means only the
        // map and this slot still hold it.
        let mut slots = guard(self.slots);
        if Arc::strong_count(&self.lock) == 2 {
            slots.remove(&self.key);
        }
    }
}

/// A workspace marked as finalizing until dropped; starts in it are refused.
struct Finalizing<'a> {
    marks: &'a Mutex<HashMap<String, usize>>,
    container: String,
}

impl<'a> Finalizing<'a> {
    fn mark(marks: &'a Mutex<HashMap<String, usize>>, container: &str) -> Self {
        *guard(marks).entry(container.to_owned()).or_default() += 1;
        Self {
            marks,
            container: container.to_owned(),
        }
    }
}

impl Drop for Finalizing<'_> {
    fn drop(&mut self) {
        let mut marks = guard(self.marks);
        if let Some(count) = marks.get_mut(&self.container) {
            *count -= 1;
            if *count == 0 {
                marks.remove(&self.container);
            }
        }
    }
}

/// Runs operations in Docker workspaces. A [`WorkspaceRef`]'s `resource_id`
/// is the container; nothing assumes a host path.
pub struct DockerOperations {
    docker: Docker,
    retention: NonZeroU64,
    max_page: NonZeroU64,
    sequence: AtomicU64,
    credentials: Option<Arc<dyn CredentialResolver>>,
    separate_channels: bool,
    /// Held only for map access, never across an await.
    records: Mutex<HashMap<Key, Arc<Operation>>>,
    /// Per-key launch locks. One spans its key's launch, credential
    /// resolution included, so racing same-key starts see the first record
    /// and launch nothing, while other keys and workspaces proceed.
    launching: Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>,
    /// Workspaces being finalized, with how many finalizes are in flight.
    finalizing: Mutex<HashMap<String, usize>>,
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
            credentials: None,
            separate_channels: false,
            records: Mutex::new(HashMap::new()),
            launching: Mutex::new(HashMap::new()),
            finalizing: Mutex::new(HashMap::new()),
        }
    }

    /// Resolve `env` credential bindings through `resolver` at launch.
    pub fn with_credentials(mut self, resolver: Arc<dyn CredentialResolver>) -> Self {
        self.credentials = Some(resolver);
        self
    }

    /// Also keep each operation's stdout and stderr apart, for
    /// [`DockerOperations::channels`]. Unbounded, unlike the observation
    /// spool: for callers that consume whole outputs.
    pub fn with_separate_channels(mut self) -> Self {
        self.separate_channels = true;
        self
    }

    /// The (stdout, stderr) bytes collected so far, each in its own order.
    pub fn channels(&self, reference: &OperationRef) -> Result<(Vec<u8>, Vec<u8>), OperationError> {
        let op = self.lookup(reference)?;
        let p = op.lock();
        let [stdout, stderr] = p.channels.clone().ok_or_else(|| {
            error(
                ErrorKind::UnknownOperation,
                format!("{} kept no separate channels", reference.operation_id),
                Some(reference),
            )
        })?;
        Ok((stdout, stderr))
    }

    /// Each binding's target and resolved value. Values reach the client's
    /// environment only; its argv names them.
    async fn resolve(&self, spec: &OperationSpec) -> Result<Vec<(String, String)>, OperationError> {
        let mut secrets = Vec::new();
        for binding in &spec.credential_bindings {
            if binding.delivery != CredentialDelivery::Env {
                return Err(error(
                    ErrorKind::UnsupportedCapability,
                    format!(
                        "credential delivery {:?} is not supported by docker",
                        binding.delivery
                    ),
                    None,
                ));
            }
            let Some(resolver) = &self.credentials else {
                return Err(error(
                    ErrorKind::RejectedPolicy,
                    "credential bindings need a CredentialResolver; none was injected",
                    None,
                ));
            };
            let value = resolver.resolve(&binding.credential).await?;
            secrets.push((binding.target.to_string(), value));
        }
        Ok(secrets)
    }

    fn lookup(&self, reference: &OperationRef) -> Result<Arc<Operation>, OperationError> {
        let key = (
            reference.workspace.resource_id.to_string(),
            reference.operation_key.to_string(),
        );
        guard(&self.records)
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

    fn client_argv(container: &str, spec: &OperationSpec, secrets: &[String]) -> Vec<String> {
        let workdir = format!(
            "/workspace/{}",
            spec.working_directory.as_str().trim_end_matches('/')
        );
        let mut argv: Vec<String> = ["exec", "-i", "-w", &workdir].map(String::from).into();
        for (name, value) in spec.environment.get() {
            argv.extend(["-e".to_owned(), format!("{name}={value}")]);
        }
        for name in secrets {
            argv.extend(["-e".to_owned(), name.clone()]);
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
        let secrets = self.resolve(&spec).await?;
        let names: Vec<String> = secrets.iter().map(|(name, _)| name.clone()).collect();
        // Every resolved value is redacted from the output before it is stored.
        let redact: Vec<Vec<u8>> = secrets.iter().map(|(_, v)| v.as_bytes().to_vec()).collect();
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
            .args(Self::client_argv(container, &spec, &names))
            .envs(secrets)
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
                spool: OperationSpool::with_redaction(self.retention, redact),
                channels: self.separate_channels.then(Default::default),
                leader: None,
                leader_known: false,
                requested: None,
                terminating: false,
                cancel_confirmed: false,
                tracked: BTreeMap::new(),
                exit_code: None,
                signal: None,
                ended_at: None,
                escalated: false,
                failures: Vec::new(),
                output_failures: Vec::new(),
                client_finished: false,
                client_code: None,
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
        async move { read_output(&op, stdout, stderr).await }
    });
    let timeout = Duration::from_secs(op.spec.timeout_seconds.get());
    let code = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(status) => status.ok().and_then(|s| s.code()),
        Err(_) => {
            let first = {
                let mut p = op.lock();
                p.requested.get_or_insert(OperationState::TimedOut);
                !std::mem::replace(&mut p.terminating, true)
            };
            if first {
                if let Err(e) = docker.terminate(&op, TIMEOUT_GRACE).await {
                    op.update(|p| {
                        p.terminating = false;
                        p.failures.push(e.message.to_string())
                    });
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
    // Settle only once no reader can append: a page marked complete must stay
    // the end of the output, so a still-open stream is stopped, not detached.
    let mut readers = readers;
    if tokio::time::timeout(FINAL_OUTPUT, &mut readers)
        .await
        .is_err()
    {
        readers.abort();
        let _ = readers.await;
        op.update(|p| {
            p.output_failures.push(format!(
                "output still open {}s after the client exited; stopped reading, \
                 later output is not collected",
                FINAL_OUTPUT.as_secs()
            ))
        });
    }
    op.update(|p| {
        p.finish_output();
        p.leader_known = true;
        p.client_finished = true;
        p.client_code = code;
    });
    docker.settle(&op, true).await;
}

/// Read both pipes to their ends, each independently and in its own order.
async fn read_output(
    op: &Operation,
    stdout: impl AsyncRead + Unpin,
    stderr: impl AsyncRead + Unpin,
) {
    tokio::join!(read_stdout(op, stdout), pump(op, stderr, Channel::Stderr));
}

async fn read_stdout(op: &Operation, stdout: impl AsyncRead + Unpin) {
    let mut reader = BufReader::new(stdout);
    let mut header = Vec::new();
    if let Err(e) = reader.read_until(b'\n', &mut header).await {
        op.update(|p| {
            p.record(&header, Channel::Stdout);
            p.leader_known = true;
            p.output_failures
                .push(format!("stdout failed before the leader line: {e}"));
        });
        return;
    }
    let text = String::from_utf8_lossy(&header);
    let fields: Vec<&str> = text.split_whitespace().collect();
    op.update(|p| {
        if fields.len() == 2 && fields.iter().all(|f| f.bytes().all(|b| b.is_ascii_digit())) {
            p.leader = Some((fields[0].to_owned(), fields[1].to_owned()));
        } else {
            // Not the wrapper's line (it failed before exec); keep it.
            p.record(&header, Channel::Stdout);
        }
        p.leader_known = true;
    });
    pump(op, reader, Channel::Stdout).await;
}

/// Append `stream` to the spool until EOF. A read error is not EOF: reading
/// stops there, nothing after it is collected, and the loss is reported as
/// `output_failure` so a complete page is never mistaken for all the output.
async fn pump(op: &Operation, mut stream: impl AsyncRead + Unpin, channel: Channel) {
    let mut buffer = vec![0; 64 * 1024];
    loop {
        match stream.read(&mut buffer).await {
            Ok(0) => return,
            Ok(n) => op.update(|p| p.record(&buffer[..n], channel)),
            Err(e) => {
                op.update(|p| {
                    let collected = p.spool.end();
                    p.output_failures.push(format!(
                        "output stream failed after {collected} bytes: {e}; \
                         later output is not collected"
                    ))
                });
                return;
            }
        }
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
            capabilities: match self.credentials {
                Some(_) => names(&["operations", "cancel", "launch_marker", "credential_env"]),
                None => names(&["operations", "cancel", "launch_marker"]),
            },
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
        let slot = KeySlot::claim(&self.launching, key.clone());
        let _launch = slot.lock.lock().await;
        if let Some(existing) = guard(&self.records).get(&key) {
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
        // Checked under the key's lock: a finalize marks first, then waits
        // for every launch in flight, so it either sees this record or this
        // start sees its mark.
        if guard(&self.finalizing).contains_key(&key.0) {
            return Err(error(
                ErrorKind::UnknownOperation,
                format!("workspace {:?} is being finalized", key.0),
                None,
            ));
        }
        let operation = self.launch(&key.0, spec).await?;
        let reference = operation.reference.clone();
        guard(&self.records).insert(key, operation);
        Ok(reference)
    }

    async fn status(&self, operation: &OperationRef) -> Result<OperationStatus, OperationError> {
        let op = self.lookup(operation)?;
        let unsettled = {
            let p = op.lock();
            p.client_finished && !p.state.is_terminal()
        };
        if unsettled {
            self.docker
                .presence(&op.container)
                .await
                .map_err(|why| error(ErrorKind::ProviderUnavailable, why, Some(&op.reference)))?;
            self.docker.settle(&op, false).await;
        }
        Ok(op.status())
    }

    async fn observe(
        &self,
        operation: &OperationRef,
        request: ObserveRequest,
    ) -> Result<ObservationPage, OperationError> {
        let op = self.lookup(operation)?;
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
        let op = self.lookup(operation)?;
        self.docker.cancel(&op, grace).await?;
        Ok(op.status())
    }

    /// Records are forgotten only once their operation has ended, so a failed
    /// finalize stays resolvable and a retry cleans up again.
    async fn finalize(&self, workspace: &WorkspaceRef) -> FinalizationOutcome {
        let container = workspace.resource_id.as_str();
        let _finalizing = Finalizing::mark(&self.finalizing, container);
        // Wait out the launches already in flight, so their records are seen.
        let in_flight: Vec<Arc<tokio::sync::Mutex<()>>> = guard(&self.launching)
            .iter()
            .filter(|((c, _), _)| c == container)
            .map(|(_, lock)| lock.clone())
            .collect();
        for lock in in_flight {
            drop(lock.lock().await);
        }
        let mine: Vec<(Key, Arc<Operation>)> = match workspace.provider.as_str() {
            "docker" => guard(&self.records)
                .iter()
                .filter(|((c, _), _)| c == workspace.resource_id.as_str())
                .map(|(key, op)| (key.clone(), op.clone()))
                .collect(),
            _ => Vec::new(),
        };
        let mut failures = Vec::new();
        for (key, op) in mine {
            match self.docker.cancel(&op, TIMEOUT_GRACE).await {
                Err(e) => failures.push(e.message.to_string()),
                Ok(()) => {
                    let mut records = guard(&self.records);
                    if records.get(&key).is_some_and(|r| Arc::ptr_eq(r, &op)) {
                        records.remove(&key);
                    }
                }
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

    /// Yields its chunks in order: `Ok` bytes, or an I/O error.
    struct Scripted(std::collections::VecDeque<std::io::Result<Vec<u8>>>);

    impl AsyncRead for Scripted {
        fn poll_read(
            mut self: std::pin::Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
            buf: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<std::io::Result<()>> {
            let next = self.0.pop_front().unwrap_or(Ok(Vec::new()));
            std::task::Poll::Ready(next.map(|bytes| buf.put_slice(&bytes)))
        }
    }

    fn scripted(chunks: Vec<std::io::Result<&[u8]>>) -> Scripted {
        Scripted(chunks.into_iter().map(|c| c.map(<[u8]>::to_vec)).collect())
    }

    fn broken() -> std::io::Error {
        std::io::Error::new(std::io::ErrorKind::BrokenPipe, "pipe broke")
    }

    fn operation() -> Operation {
        let spec: OperationSpec = serde_json::from_value(serde_json::json!({
            "execution_id": "e1",
            "operation_key": "k1",
            "argv": ["true"],
            "working_directory": ".",
            "environment": {},
            "credential_bindings": [],
            "timeout_seconds": 60,
            "metadata": {},
        }))
        .unwrap();
        Operation {
            reference: serde_json::from_value(serde_json::json!({
                "workspace": {"provider": "docker", "resource_id": "c1"},
                "operation_id": "op-1",
                "operation_key": "k1",
            }))
            .unwrap(),
            spec,
            container: "c1".into(),
            started_at: now_rfc3339(),
            progress: Mutex::new(Progress {
                state: OperationState::Running,
                spool: OperationSpool::new(NonZeroU64::new(1 << 20).unwrap()),
                channels: Some(Default::default()),
                leader: None,
                leader_known: false,
                requested: None,
                terminating: false,
                cancel_confirmed: false,
                tracked: BTreeMap::new(),
                exit_code: None,
                signal: None,
                ended_at: None,
                escalated: false,
                failures: Vec::new(),
                output_failures: Vec::new(),
                client_finished: false,
                client_code: None,
            }),
            changed: Notify::new(),
        }
    }

    fn output_failure(op: &Operation) -> Option<String> {
        op.status().metadata.get().get("output_failure").cloned()
    }

    fn collected(op: &Operation) -> Vec<u8> {
        let page = op
            .lock()
            .spool
            .page(&op.reference, OperationState::Succeeded, None, 1 << 16)
            .unwrap();
        base64::Engine::decode(
            &base64::engine::general_purpose::STANDARD,
            page.data_b64.as_str(),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn a_stream_error_stops_collection_and_is_reported_not_taken_for_eof() {
        let op = operation();
        let stream = scripted(vec![Ok(b"before\n"), Err(broken()), Ok(b"after\n")]);
        pump(&op, stream, Channel::Stdout).await;
        assert_eq!(collected(&op), b"before\n");
        let failure = output_failure(&op).expect("the stream error is reported");
        assert!(failure.contains("after 7 bytes: pipe broke"), "{failure}");
        assert!(failure.contains("not collected"), "{failure}");
    }

    #[tokio::test]
    async fn a_resolved_value_never_reaches_the_spool_pages_or_channels() {
        // Invented. Printed whole, split across two reads, and repeated.
        const CANARY: &[u8] = b"canary-INVENTED-sk-7f3a";
        let op = operation();
        op.lock().spool = OperationSpool::with_redaction(
            NonZeroU64::new(1 << 20).unwrap(),
            vec![CANARY.to_vec()],
        );
        let (head, tail) = CANARY.split_at(11);
        let whole = [b"whole ", CANARY, b"\nsplit ", head].concat();
        let rest = [tail, b"\n"].concat();
        pump(&op, scripted(vec![Ok(&whole), Ok(&rest)]), Channel::Stdout).await;
        let repeated = [CANARY, CANARY, b" end:", head].concat();
        pump(&op, scripted(vec![Ok(&repeated)]), Channel::Stderr).await;
        op.update(|p| p.finish_output());
        let (stdout, stderr) = channels(&op);
        assert_eq!(stdout, b"whole [REDACTED]\nsplit [REDACTED]\n");
        assert_eq!(stderr, [b"[REDACTED][REDACTED] end:", head].concat());
        let stored = collected(&op);
        assert_eq!(stored, [stdout, stderr].concat());
        assert!(!stored.windows(CANARY.len()).any(|w| w == CANARY));
    }

    #[tokio::test]
    async fn a_clean_eof_reports_no_output_failure() {
        let op = operation();
        pump(&op, scripted(vec![Ok(b"all\n")]), Channel::Stdout).await;
        assert_eq!(collected(&op), b"all\n");
        assert_eq!(output_failure(&op), None);
    }

    #[tokio::test]
    async fn a_stdout_error_before_the_leader_line_is_reported_and_stops_reading() {
        let op = operation();
        let stream = scripted(vec![Ok(b"partial"), Err(broken()), Ok(b"\nafter\n")]);
        read_stdout(&op, stream).await;
        assert_eq!(collected(&op), b"partial");
        assert!(op.lock().leader_known);
        assert!(op.lock().leader.is_none());
        let failure = output_failure(&op).expect("the stream error is reported");
        assert!(
            failure.contains("before the leader line: pipe broke"),
            "{failure}"
        );
    }

    /// Which pipe the test lets the reader see first.
    #[derive(Clone, Copy)]
    enum First {
        Stdout,
        Stderr,
    }

    /// Drive `read_output` over real async pipes, forcing `first`'s bytes to
    /// be read in full before the other pipe carries anything.
    async fn read_scheduled(first: First, stdout: &[u8], stderr: &[u8]) -> Operation {
        let op = Arc::new(operation());
        let (mut out_w, out_r) = tokio::io::duplex(1 << 16);
        let (mut err_w, err_r) = tokio::io::duplex(1 << 16);
        let readers = tokio::spawn({
            let op = op.clone();
            async move { read_output(&op, out_r, err_r).await }
        });
        let mut writes: [(&mut tokio::io::DuplexStream, &[u8], Channel); 2] = [
            (&mut out_w, stdout, Channel::Stdout),
            (&mut err_w, stderr, Channel::Stderr),
        ];
        if let First::Stderr = first {
            writes.swap(0, 1);
        }
        for (pipe, bytes, channel) in writes {
            pipe.write_all(bytes).await.unwrap();
            pipe.shutdown().await.unwrap();
            // Wait until this pipe is read whole before the other carries
            // anything; a trusted header is consumed, not recorded.
            let header = bytes.iter().position(|b| *b == b'\n').map_or(0, |i| i + 1);
            let read = |p: &Progress| {
                let kept = p.channels.as_ref().unwrap()[channel as usize].len();
                let consumed = match channel {
                    Channel::Stdout if p.leader.is_some() => header,
                    _ => 0,
                };
                kept + consumed == bytes.len()
            };
            assert!(op.wait_for(Duration::from_secs(5), read).await);
        }
        readers.await.unwrap();
        Arc::try_unwrap(op).ok().unwrap()
    }

    fn channels(op: &Operation) -> (Vec<u8>, Vec<u8>) {
        let [stdout, stderr] = op.lock().channels.clone().unwrap();
        (stdout, stderr)
    }

    #[tokio::test]
    async fn each_channel_keeps_its_own_order_under_either_reader_schedule() {
        let header = b"4242 123456\n";
        let stdout: Vec<u8> = [&header[..], b"out1\nout2\n"].concat();
        for first in [First::Stdout, First::Stderr] {
            let op = read_scheduled(first, &stdout, b"err1\nerr2\n").await;
            assert_eq!(
                op.lock().leader,
                Some(("4242".to_owned(), "123456".to_owned()))
            );
            assert_eq!(
                channels(&op),
                (b"out1\nout2\n".to_vec(), b"err1\nerr2\n".to_vec())
            );
            // The combined stream is the order the readers received: which
            // pipe came first is the schedule's, never assumed.
            let expected: &[u8] = match first {
                First::Stdout => b"out1\nout2\nerr1\nerr2\n",
                First::Stderr => b"err1\nerr2\nout1\nout2\n",
            };
            assert_eq!(collected(&op), expected);
            assert_eq!(output_failure(&op), None);
        }
    }

    #[tokio::test]
    async fn stderr_before_the_header_is_never_taken_for_it() {
        let op = read_scheduled(First::Stderr, b"7 8\n", b"1 2\n").await;
        assert_eq!(op.lock().leader, Some(("7".to_owned(), "8".to_owned())));
        assert_eq!(channels(&op), (Vec::new(), b"1 2\n".to_vec()));
    }

    #[tokio::test]
    async fn one_channel_alone_is_collected_whole() {
        let op = read_scheduled(First::Stdout, b"7 8\nonly out\n", b"").await;
        assert_eq!(channels(&op), (b"only out\n".to_vec(), Vec::new()));
        let op = read_scheduled(First::Stderr, b"7 8\n", b"only err\n").await;
        assert_eq!(channels(&op), (Vec::new(), b"only err\n".to_vec()));
        assert_eq!(collected(&op), b"only err\n");
    }

    #[tokio::test]
    async fn a_second_chunk_failure_on_one_channel_keeps_the_other_whole() {
        let op = operation();
        let stdout = scripted(vec![Ok(b"7 8\nfirst\n"), Err(broken()), Ok(b"lost\n")]);
        let stderr = scripted(vec![Ok(b"e1\n"), Ok(b"e2\n")]);
        read_output(&op, stdout, stderr).await;
        assert_eq!(channels(&op), (b"first\n".to_vec(), b"e1\ne2\n".to_vec()));
        let failure = output_failure(&op).expect("the stream error is reported");
        assert!(failure.contains("pipe broke"), "{failure}");
    }

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
        // ...but it was adopted by init after the wrapper started, so it may
        // be another descendant and confirmation fails closed.
        assert_eq!(unattributed(&reused, "500", &tracked), ["12"]);
    }

    #[test]
    fn an_untracked_orphan_of_init_since_the_wrapper_is_unattributed() {
        // Detached and orphaned before cancel first looked: no member, the
        // group is gone, yet it may be ours.
        let rows = table(&["12 1 12 502"]);
        let none = BTreeMap::new();
        assert!(members(&rows, "10", "500", &none).is_empty());
        assert!(all_gone(&rows, "10", "500", &none));
        assert_eq!(unattributed(&rows, "500", &none), ["12"]);
        // An equal start tick is included; an earlier start, a tracked
        // identity, and a process with a living parent are not.
        let rows = table(&[
            "13 1 13 500",
            "14 1 14 499",
            "15 1 15 600",
            "16 40 16 700",
            "40 0 40 1",
        ]);
        let tracked = BTreeMap::from([("15".to_owned(), "600".to_owned())]);
        assert_eq!(unattributed(&rows, "500", &tracked), ["13"]);
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
    fn container_inspect_answers_are_classified() {
        assert_eq!(presence(0, "running\n"), Ok(Presence::Running));
        assert_eq!(presence(0, "paused\n"), Ok(Presence::Running));
        assert_eq!(presence(0, "removing\n"), Ok(Presence::Removing));
        assert_eq!(presence(0, "exited\n"), Ok(Presence::Stopped));
        assert_eq!(
            presence(1, "Error response from daemon: No such container: c1\n"),
            Ok(Presence::Gone)
        );
        assert!(
            presence(
                1,
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
            )
            .is_err()
        );
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
