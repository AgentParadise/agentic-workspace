//! APSS EXP-V1-0006 0.2.1 lifecycle-hazard fixtures (LH-1..LH-7) for Rust providers.
//!
//! Loads the vendored `behaviour/hazard-*.json` fixtures, drives each step
//! through any [`SupportsOperations`] provider with [`drive_hazard`], and
//! judges the measured outcome with [`judge`]. A [`HazardHarness`] (the
//! hazard counterpart of [`crate::behaviour::BehaviourHarness`]) establishes
//! each step's `given` conditions and measures `descendant_alive` (see
//! [`live_processes`]) and launches; nothing here takes liveness from the
//! fixture. A condition the harness cannot establish, or a call the trait has
//! no method for, ends the run as [`HazardVerdict::NotRun`], never a pass.
//!
//! The Python harness (`lib/python/agentic_isolation/tests/test_behaviour_hazards.py`)
//! drives both Python providers with the same matching rule.

use agentic_workspace_core::operations::{
    ObserveRequest, OperationError, OperationRef, OperationSpec, SupportsOperations, WorkspaceRef,
};
use serde::Deserialize;
use serde_json::Value;
use std::fmt;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// The vendored fixture directory (APSS ee926c6, see the contract PROVENANCE.md).
pub fn vendored_behaviour_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../lib/python/agentic_isolation/tests/fixtures/apss_exp_v1_0006/behaviour")
}

#[derive(Debug, Clone, Deserialize)]
pub struct HazardFixture {
    pub hazard: String,
    pub description: String,
    pub steps: Vec<HazardStep>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HazardStep {
    pub call: String,
    #[serde(default)]
    pub args: Value,
    #[serde(default)]
    pub given: Vec<String>,
    pub settles_within_seconds: Option<f64>,
    expect: Option<Expectation>,
    expect_one_of: Option<Vec<Expectation>>,
    pub launches: Option<u32>,
    pub operation_records: Option<u32>,
    #[serde(default)]
    pub repeat_until_complete: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Expectation {
    pub schema: Option<String>,
    pub document: Value,
    pub descendant_alive: Option<bool>,
}

impl HazardStep {
    /// Every correct response: `expect_one_of`, or the single `expect`.
    pub fn alternatives(&self) -> &[Expectation] {
        match (&self.expect_one_of, &self.expect) {
            (Some(many), _) => many,
            (None, Some(one)) => std::slice::from_ref(one),
            (None, None) => &[],
        }
    }
}

/// What a provider call did, reduced to the fields a hazard judges.
#[derive(Debug, Clone, PartialEq)]
pub enum Outcome {
    /// A returned document: status, observation page, finalization or probe.
    Document(Value),
    /// A typed error: its kind and whether it carried the operation ref.
    Error { kind: String, has_ref: bool },
}

/// What the driver measured around one call.
#[derive(Debug, Clone, PartialEq)]
pub struct Measured {
    pub outcome: Outcome,
    pub elapsed: Duration,
    pub descendant_alive: bool,
    pub launches: u32,
    pub operation_records: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Mismatch {
    Unsettled { bound: f64, elapsed: Duration },
    NoAlternative { measured: Box<Measured> },
    Launches { expected: u32, measured: u32 },
    OperationRecords { expected: u32, measured: u32 },
}

impl fmt::Display for Mismatch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsettled { bound, elapsed } => {
                write!(f, "did not settle within {bound}s (took {elapsed:?})")
            }
            Self::NoAlternative { measured } => write!(
                f,
                "{:?} with measured descendant_alive={} matched no alternative",
                measured.outcome, measured.descendant_alive
            ),
            Self::Launches { expected, measured } => {
                write!(f, "measured {measured} launches, fixture says {expected}")
            }
            Self::OperationRecords { expected, measured } => {
                write!(
                    f,
                    "provider holds {measured} operation records, fixture says {expected}"
                )
            }
        }
    }
}

impl std::error::Error for Mismatch {}

/// Outcome fields that identify a response, whatever its prose.
const STABLE_FIELDS: [&str; 7] = [
    "state",
    "complete",
    "gap",
    "loss_detected",
    "exists",
    "regular_file",
    "operation_key",
];

impl Mismatch {
    /// The measured outcome as stable fields, never prose: what a pinned
    /// finding names, so a miss that moves or changes shape no longer matches.
    pub fn outcome(&self) -> String {
        match self {
            Self::Unsettled { .. } => "unsettled".to_owned(),
            Self::NoAlternative { measured } => {
                let fields = match &measured.outcome {
                    Outcome::Error { kind, has_ref } => format!("kind={kind} ref={has_ref}"),
                    Outcome::Document(doc) => STABLE_FIELDS
                        .iter()
                        .filter_map(|key| Some(format!("{key}={}", doc.get(*key)?)))
                        .collect::<Vec<_>>()
                        .join(" "),
                };
                format!("{fields} alive={}", measured.descendant_alive)
            }
            Self::Launches { measured, .. } => format!("launches={measured}"),
            Self::OperationRecords { measured, .. } => format!("operation_records={measured}"),
        }
    }
}

const OBSERVATION_FIELDS: [&str; 4] = ["state", "complete", "gap", "loss_detected"];

/// Whether `measured` is one of `step`'s correct responses, judged by outcome
/// (state, error kind and ref, page fields), never by message prose. `Ok`
/// carries the index of the alternative it matched.
pub fn judge(step: &HazardStep, measured: &Measured) -> Result<usize, Mismatch> {
    if let Some(bound) = step.settles_within_seconds {
        if measured.elapsed.as_secs_f64() > bound {
            return Err(Mismatch::Unsettled {
                bound,
                elapsed: measured.elapsed,
            });
        }
    }
    let Some(matched) = matches(step.alternatives(), measured) else {
        return Err(Mismatch::NoAlternative {
            measured: Box::new(measured.clone()),
        });
    };
    if let Some(expected) = step.launches.filter(|n| *n != measured.launches) {
        return Err(Mismatch::Launches {
            expected,
            measured: measured.launches,
        });
    }
    if let Some(expected) = step
        .operation_records
        .filter(|n| *n != measured.operation_records)
    {
        return Err(Mismatch::OperationRecords {
            expected,
            measured: measured.operation_records,
        });
    }
    Ok(matched)
}

/// The index of the first alternative `measured` matches.
fn matches(alternatives: &[Expectation], measured: &Measured) -> Option<usize> {
    alternatives
        .iter()
        .position(|alt| matches_one(alt, measured))
}

fn matches_one(alternative: &Expectation, measured: &Measured) -> bool {
    if alternative
        .descendant_alive
        .is_some_and(|alive| alive != measured.descendant_alive)
    {
        return false;
    }
    let want = &alternative.document;
    if alternative
        .schema
        .as_deref()
        .is_some_and(|s| s.ends_with("#/$defs/operationRef"))
    {
        // Workspace and operation ids are provider-chosen; the key is the caller's.
        return matches!(&measured.outcome, Outcome::Document(got)
            if got.get("operation_id").is_some()
                && got.get("operation_key") == want.get("operation_key"));
    }
    match (&measured.outcome, want.get("kind")) {
        (Outcome::Error { kind, has_ref }, Some(want_kind)) => {
            want_kind.as_str() == Some(kind) && *has_ref == want.get("ref").is_some()
        }
        (Outcome::Error { .. }, None) | (Outcome::Document(_), Some(_)) => false,
        (Outcome::Document(got), None) => match want.get("schema").and_then(Value::as_str) {
            Some("apss.workspace-status/v1") | Some("apss.workspace-finalization/v1") => {
                got.get("state") == want.get("state")
            }
            Some("apss.workspace-observation/v1") => OBSERVATION_FIELDS
                .iter()
                .all(|key| got.get(key) == want.get(key)),
            // An operationRef, or a fixture-level file probe: every expected field.
            _ => want
                .as_object()
                .is_some_and(|fields| fields.iter().all(|(k, v)| got.get(k) == Some(v))),
        },
    }
}

/// Every hazard fixture in `dir`, sorted by hazard id.
pub fn load_hazards(dir: &Path) -> std::io::Result<Vec<HazardFixture>> {
    let mut fixtures = Vec::new();
    for entry in std::fs::read_dir(dir)? {
        let path = entry?.path();
        let is_hazard = path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("hazard-") && n.ends_with(".json"));
        if is_hazard {
            let text = std::fs::read_to_string(&path)?;
            let fixture = serde_json::from_str(&text).map_err(|e| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("{}: {e}", path.display()),
                )
            })?;
            fixtures.push(fixture);
        }
    }
    fixtures.sort_by(|a: &HazardFixture, b| a.hazard.cmp(&b.hazard));
    Ok(fixtures)
}

/// The provider under test and the test environment around one hazard.
pub trait HazardHarness {
    type Provider: SupportsOperations;
    fn provider(&self) -> &Self::Provider;
    fn workspace(&self) -> WorkspaceRef;
    /// The spec to start: the fixture's, with its `argv` replaced by a
    /// workload that produces `hazard`'s conditions, carrying the harness's
    /// liveness token.
    fn spec(&self, hazard: &str, fixture_spec: &Value) -> OperationSpec;
    /// Make `given` true before the step runs, or say why it cannot be.
    /// `caller_cancelled_during_confirmation` is the runner's, not this.
    fn establish(&self, hazard: &str, given: &str) -> Result<(), String>;
    /// Workloads the provider launched for this run.
    fn launches(&self) -> u32;
    /// Live processes carrying this run's token, detached ones included.
    fn descendant_alive(&self) -> Result<bool, Unmeasurable>;
    /// Run alongside the step's call, for a `given` that only holds while
    /// the call is in flight (LH-4's destroy during credential resolution).
    /// An `Err` says it could not be made true, and the step is not run.
    fn alongside(&self, _hazard: &str, _given: &str) -> impl Future<Output = Result<(), String>> {
        async { Ok(()) }
    }
    /// Answer a file call (`file_exists`, `read_file`) through the provider's
    /// file surface. `Err` says there is none, and the step is not run.
    fn file(&self, call: &str, _path: &str) -> impl Future<Output = Result<Outcome, String>> {
        let reason = format!("this harness has no file surface for {call}");
        async move { Err(reason) }
    }
    /// Operation records the provider holds for the workspace, when it
    /// exposes that; `None` makes a step that counts them not run.
    fn operation_records(&self) -> impl Future<Output = Option<u32>>;
    /// Whether `hazard`'s workload is short and the fixture's next steps
    /// assume it has exited: `start` then waits for a terminal status.
    fn awaits_exit(&self, hazard: &str) -> bool;
}

/// How one hazard went for one provider.
#[derive(Debug)]
pub enum HazardVerdict {
    /// Every step matched; `matched[i]` is the alternative step `i` matched.
    Pass { matched: Vec<usize> },
    /// A measured outcome the fixture forbids: a provider finding.
    Fail {
        step: usize,
        call: String,
        mismatch: Mismatch,
    },
    /// The step could not be executed as the fixture says. Not a pass.
    NotRun {
        step: usize,
        call: String,
        reason: String,
    },
}

const CALLER_ABANDONS: &str = "caller_cancelled_during_confirmation";

fn error_outcome(error: &OperationError) -> Outcome {
    Outcome::Error {
        kind: serde_json::to_value(error.kind)
            .ok()
            .and_then(|v| v.as_str().map(str::to_owned))
            .unwrap_or_default(),
        has_ref: error.operation.is_some(),
    }
}

fn document<T: serde::Serialize>(result: Result<T, OperationError>) -> Outcome {
    match result {
        Ok(value) => Outcome::Document(serde_json::to_value(value).expect("serialisable")),
        Err(error) => error_outcome(&error),
    }
}

/// Run every step of `fixture` against `harness`'s provider, stopping at the
/// first step that fails or cannot run.
pub async fn drive_hazard<H: HazardHarness>(fixture: &HazardFixture, harness: &H) -> HazardVerdict {
    let provider = harness.provider();
    let hazard = fixture.hazard.as_str();
    let mut operation: Option<OperationRef> = None;
    let mut cursor: Option<String> = None;
    let mut matched = Vec::with_capacity(fixture.steps.len());
    for (index, step) in fixture.steps.iter().enumerate() {
        let not_run = |reason: String| HazardVerdict::NotRun {
            step: index,
            call: step.call.clone(),
            reason,
        };
        for given in step.given.iter().filter(|g| *g != CALLER_ABANDONS) {
            if let Err(reason) = harness.establish(hazard, given) {
                return not_run(format!("given {given}: {reason}"));
            }
        }
        let bound = Duration::from_secs_f64(step.settles_within_seconds.unwrap_or(30.0));
        let began = std::time::Instant::now();
        let call = async {
            match step.call.as_str() {
                "start" => {
                    let spec = harness.spec(hazard, &step.args["spec"]);
                    let result = provider.start(&harness.workspace(), spec).await;
                    if let Ok(started) = &result {
                        operation = Some(started.clone());
                        while harness.awaits_exit(hazard)
                            && !provider
                                .status(started)
                                .await
                                .is_ok_and(|s| s.state.is_terminal())
                        {
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    }
                    Ok(document(result))
                }
                "finalize" => Ok(Outcome::Document(
                    serde_json::to_value(provider.finalize(&harness.workspace()).await)
                        .expect("serialisable"),
                )),
                call => {
                    let Some(op) = operation.as_ref() else {
                        return Err(format!("{call} before a successful start"));
                    };
                    match call {
                        "status" => Ok(document(provider.status(op).await)),
                        "cancel" if step.given.iter().any(|g| g == CALLER_ABANDONS) => {
                            // The caller drops the cancel mid-confirmation.
                            let abandoned = tokio::time::timeout(
                                Duration::from_millis(300),
                                provider.cancel(op, Duration::from_secs(3)),
                            )
                            .await;
                            Ok(match abandoned {
                                Ok(result) => document(result),
                                Err(_) => Outcome::Error {
                                    kind: "cancelled".into(),
                                    has_ref: true,
                                },
                            })
                        }
                        "cancel" => Ok(document(provider.cancel(op, Duration::from_secs(1)).await)),
                        "observe" => loop {
                            let request = ObserveRequest {
                                cursor: cursor.clone(),
                                max_bytes: 1 << 16,
                                wait: Duration::from_secs(1),
                            };
                            match provider.observe(op, request).await {
                                Ok(page) => {
                                    cursor = Some(page.next_cursor.to_string());
                                    if page.complete || !step.repeat_until_complete {
                                        break Ok(document(Ok(page)));
                                    }
                                }
                                Err(error) => break Ok(error_outcome(&error)),
                            }
                        },
                        "file_exists" | "read_file" => {
                            let path = step.args["path"].as_str().unwrap_or_default();
                            harness.file(call, path).await
                        }
                        other => Err(format!(
                            "SupportsOperations has no {other}; no Rust provider surface serves it"
                        )),
                    }
                }
            }
        };
        let during = async {
            for given in &step.given {
                harness.alongside(hazard, given).await?;
            }
            Ok::<(), String>(())
        };
        let outcome = match tokio::time::timeout(bound, async { tokio::join!(call, during) }).await
        {
            Ok((_, Err(reason))) => return not_run(reason),
            Ok((Ok(outcome), Ok(()))) => outcome,
            Ok((Err(reason), Ok(()))) => return not_run(reason),
            Err(_) => {
                return HazardVerdict::Fail {
                    step: index,
                    call: step.call.clone(),
                    mismatch: Mismatch::Unsettled {
                        bound: bound.as_secs_f64(),
                        elapsed: began.elapsed(),
                    },
                };
            }
        };
        let elapsed = began.elapsed();
        let descendant_alive = match harness.descendant_alive() {
            Ok(alive) => alive,
            Err(unmeasurable) => return not_run(unmeasurable.to_string()),
        };
        // Probed only when the step counts records: the probe may call the
        // provider (Docker's reads `status`), and LH-5 forbids a status call
        // before its observe. It is bounded like the call it measures.
        let operation_records = match step.operation_records {
            None => 0,
            Some(_) => match tokio::time::timeout(bound, harness.operation_records()).await {
                Ok(Some(measured)) => measured,
                Ok(None) => {
                    return not_run("the provider does not expose its operation records".into());
                }
                Err(_) => {
                    return not_run(format!(
                        "the operation record probe did not answer within {}s",
                        bound.as_secs_f64()
                    ));
                }
            },
        };
        let measured = Measured {
            outcome,
            elapsed,
            descendant_alive,
            launches: settled_launches(harness, step.launches).await,
            operation_records,
        };
        match judge(step, &measured) {
            Ok(alternative) => matched.push(alternative),
            Err(mismatch) => {
                return HazardVerdict::Fail {
                    step: index,
                    call: step.call.clone(),
                    mismatch,
                };
            }
        }
    }
    HazardVerdict::Pass { matched }
}

/// The launch count once it stops moving: up to 2s for an expected launch
/// to be logged, then a quiet period so a late extra launch is counted too.
async fn settled_launches<H: HazardHarness>(harness: &H, expected: Option<u32>) -> u32 {
    let Some(expected) = expected else {
        return harness.launches();
    };
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    while harness.launches() != expected && tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    harness.launches()
}

/// `descendant_alive` cannot be measured on this host: there is no `/proc`
/// process table to read, or a process that could be a workload hides its
/// environment (hidepid, another uid). A driver must report this, never read
/// it as "no live descendants", or a confirmed cancel would pass vacuously.
#[derive(Debug)]
pub struct Unmeasurable(pub std::io::Error);

impl fmt::Display for Unmeasurable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "descendant liveness is unmeasurable here: {}; run the hazard harness \
             on Linux where the workload's /proc entries are readable \
             (AgentParadise/agentic-workspace#68)",
            self.0
        )
    }
}

impl std::error::Error for Unmeasurable {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.0)
    }
}

/// Live, non-zombie pids whose environment carries `variable=token`: the
/// measured `descendant_alive`, detached descendants included. Linux only:
/// elsewhere this is [`Unmeasurable`], not an empty list.
///
/// Fails closed: a process that could be a workload but whose environment
/// cannot be read is [`Unmeasurable`], never "not ours". A process older than
/// this one cannot be a workload it launched, and one that exits during the
/// scan, or is a zombie, is gone; none is a candidate.
pub fn live_processes(variable: &str, token: &str) -> Result<Vec<u32>, Unmeasurable> {
    live_processes_in(Path::new("/proc"), variable, token)
}

/// [`live_processes`] over the process table mounted at `proc`.
pub fn live_processes_in(
    proc: &Path,
    variable: &str,
    token: &str,
) -> Result<Vec<u32>, Unmeasurable> {
    let needle = format!("{variable}={token}");
    let entries = std::fs::read_dir(proc).map_err(|e| {
        Unmeasurable(std::io::Error::new(
            e.kind(),
            format!("{} is unreadable ({e})", proc.display()),
        ))
    })?;
    let me = std::process::id();
    let began = started(&proc.join("self")).map_err(Unmeasurable)?;
    let mut live = Vec::new();
    for pid in entries
        .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse::<u32>().ok())
        .filter(|pid| *pid != me)
    {
        let entry = proc.join(pid.to_string());
        let read = || -> std::io::Result<Option<u32>> {
            if started(&entry)? < began || zombie(&entry)? {
                return Ok(None);
            }
            let environ = match std::fs::read(entry.join("environ")) {
                Ok(environ) => environ,
                // A process that died after the check hides its environ too.
                Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                    return if zombie(&entry)? { Ok(None) } else { Err(e) };
                }
                Err(e) => return Err(e),
            };
            let ours = environ
                .split(|b| *b == 0)
                .any(|var| var == needle.as_bytes());
            Ok(ours.then_some(pid))
        };
        match read() {
            Ok(found) => live.extend(found),
            Err(e) if exited(&e) => {}
            Err(e) => {
                return Err(Unmeasurable(std::io::Error::new(
                    e.kind(),
                    format!("{} is unreadable ({e})", entry.display()),
                )));
            }
        }
    }
    Ok(live)
}

/// When the process at `entry` began, in clock ticks since boot (proc(5)
/// `stat` field 22).
fn started(entry: &Path) -> std::io::Result<u64> {
    let stat = std::fs::read_to_string(entry.join("stat"))?;
    stat.rsplit_once(')')
        .and_then(|(_, rest)| rest.split_whitespace().nth(19)?.parse().ok())
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("{}/stat has no start time", entry.display()),
            )
        })
}

fn zombie(entry: &Path) -> std::io::Result<bool> {
    Ok(std::fs::read_to_string(entry.join("status"))?.contains("\nState:\tZ"))
}

/// The process left the table between listing and reading: gone, not hidden.
fn exited(error: &std::io::Error) -> bool {
    error.kind() == std::io::ErrorKind::NotFound || error.raw_os_error() == Some(3)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn lh(id: &str) -> HazardFixture {
        load_hazards(&vendored_behaviour_dir())
            .unwrap()
            .into_iter()
            .find(|f| f.hazard == id)
            .unwrap()
    }

    fn cancelled() -> Outcome {
        Outcome::Document(json!({"schema": "apss.workspace-status/v1", "state": "cancelled"}))
    }

    fn measured(outcome: Outcome, descendant_alive: bool) -> Measured {
        Measured {
            outcome,
            elapsed: Duration::from_millis(10),
            descendant_alive,
            launches: 1,
            operation_records: 1,
        }
    }

    #[test]
    fn all_seven_vendored_hazards_load_with_their_alternatives() {
        let ids: Vec<String> = load_hazards(&vendored_behaviour_dir())
            .unwrap()
            .into_iter()
            .map(|f| f.hazard)
            .collect();
        assert_eq!(
            ids,
            ["LH-1", "LH-2", "LH-3", "LH-4", "LH-5", "LH-6", "LH-7"]
        );
        assert_eq!(lh("LH-1").steps[1].alternatives().len(), 2);
    }

    #[test]
    fn lh1_rejects_a_confirmed_cancel_while_a_descendant_lives() {
        let step = &lh("LH-1").steps[1];
        assert_eq!(judge(step, &measured(cancelled(), false)), Ok(0));
        assert!(matches!(
            judge(step, &measured(cancelled(), true)),
            Err(Mismatch::NoAlternative { .. })
        ));
    }

    #[test]
    fn lh1_accepts_the_unconfirmed_outcome_only_with_its_ref() {
        let step = &lh("LH-1").steps[1];
        let timeout = |has_ref| Outcome::Error {
            kind: "timeout".into(),
            has_ref,
        };
        assert_eq!(judge(step, &measured(timeout(true), true)), Ok(1));
        assert!(judge(step, &measured(timeout(false), true)).is_err());
    }

    #[test]
    fn a_step_past_its_settlement_bound_fails() {
        let step = &lh("LH-1").steps[1];
        let mut slow = measured(cancelled(), false);
        slow.elapsed = Duration::from_secs(61);
        assert!(matches!(
            judge(step, &slow),
            Err(Mismatch::Unsettled { .. })
        ));
    }

    #[test]
    fn lh4_counts_launches_and_records_after_destroy() {
        let step = &lh("LH-4").steps[0];
        let lost = Outcome::Error {
            kind: "resource_lost".into(),
            has_ref: false,
        };
        let mut clean = measured(lost, false);
        (clean.launches, clean.operation_records) = (0, 0);
        assert_eq!(judge(step, &clean), Ok(0));
        let mut launched = clean.clone();
        launched.launches = 1;
        assert!(matches!(
            judge(step, &launched),
            Err(Mismatch::Launches { .. })
        ));
    }

    #[test]
    fn lh6_completion_without_loss_detected_fails() {
        let step = &lh("LH-6").steps[3];
        let page = |loss: bool| {
            let mut doc = json!({"schema": "apss.workspace-observation/v1",
                "state": "succeeded", "complete": true});
            if loss {
                doc["loss_detected"] = json!(true);
            }
            Outcome::Document(doc)
        };
        assert_eq!(judge(step, &measured(page(true), false)), Ok(0));
        assert!(judge(step, &measured(page(false), false)).is_err());
    }

    /// Kills and reaps the child even when an assertion panics first.
    #[cfg(target_os = "linux")]
    struct Reaped(std::process::Child);

    #[cfg(target_os = "linux")]
    impl Drop for Reaped {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn live_processes_measures_a_tokened_child_and_not_after_it_exits() {
        let token = format!("rust-hazard-{}", std::process::id());
        let child = Reaped(
            std::process::Command::new("sleep")
                .arg("30")
                .env("AW_HARNESS_RUN", &token)
                .spawn()
                .unwrap(),
        );
        let pid = child.0.id();
        assert_eq!(live_processes("AW_HARNESS_RUN", &token).unwrap(), [pid]);
        drop(child);
        assert!(live_processes("AW_HARNESS_RUN", &token).unwrap().is_empty());
    }

    /// A process table whose only candidate hides its environment: an
    /// `environ` that cannot be read as a file, whoever runs the test.
    #[cfg(target_os = "linux")]
    #[test]
    fn an_unreadable_environ_is_unmeasurable_not_absent() {
        let proc = std::env::temp_dir().join(format!("aw-fake-proc-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&proc);
        let stat = |ticks: u64| format!("1 (sh) S {}{ticks} 0\n", "0 ".repeat(18));
        std::fs::create_dir_all(proc.join("self")).unwrap();
        std::fs::write(proc.join("self/stat"), stat(100)).unwrap();
        let hidden = proc.join("4242");
        std::fs::create_dir_all(hidden.join("environ")).unwrap();
        std::fs::write(hidden.join("stat"), stat(200)).unwrap();
        std::fs::write(hidden.join("status"), "Name:\tsh\nState:\tS\n").unwrap();
        let measured = live_processes_in(&proc, "AW_HARNESS_RUN", "t");
        let error = measured.expect_err("an unreadable environ was counted as not ours");
        assert!(error.to_string().contains("4242"), "{error}");
        // Older than the measuring process: not a candidate, so not unmeasurable.
        std::fs::write(hidden.join("stat"), stat(50)).unwrap();
        assert_eq!(
            live_processes_in(&proc, "AW_HARNESS_RUN", "t").unwrap(),
            Vec::<u32>::new()
        );
        std::fs::remove_dir_all(&proc).unwrap();
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn live_processes_is_unmeasurable_without_proc() {
        assert!(live_processes("AW_HARNESS_RUN", "any").is_err());
    }
}
