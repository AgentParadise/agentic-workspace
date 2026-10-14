//! Execute a vendored APSS behaviour fixture against any [`SupportsOperations`]
//! provider, step by step from its file.
//!
//! The fixture's own specs, credential bindings and launch markers are what
//! the provider starts. A [`BehaviourHarness`] supplies what the fixture
//! assumes but a provider cannot: a workload for the fixture's `argv[0]` and
//! a launch count. Provider-chosen ids, messages, timestamps and metadata are
//! not compared. The fixture's cursors are names: each is bound to the cursor
//! the provider issued at the same point, so a provider's cursors stay opaque.
//!
//! Byte offsets in a fixture count the workload's output. The launch marker
//! the contract requires on the stream before it (plan section 4.2) is read
//! first, checked, and stands before the fixture's `g1:0`.

use crate::operations::fixture_root;
use agentic_workspace_core::operations::{
    ObservationPage, ObserveRequest, OperationError, OperationRef, OperationSpec, OperationStatus,
    SupportsOperations, WorkspaceRef, WorkspaceResult,
};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;
use std::collections::BTreeMap;
use std::time::Duration;

/// What a fixture's `argv[0]` does once launched and released: write
/// `output` to stdout, then exit 0 when `exits`, else keep running.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Workload {
    pub output: Vec<u8>,
    pub exits: bool,
}

/// The provider under test and the test environment around it.
pub trait BehaviourHarness {
    type Provider: SupportsOperations;
    fn provider(&self) -> &Self::Provider;
    /// Make the fixture's `argv[0]` behave as `workload`, held until
    /// [`BehaviourHarness::release`].
    fn program(&self, workload: &Workload);
    fn release(&self);
    /// How many times the fixture's `argv[0]` has been launched.
    fn launches(&self) -> usize;
}

/// The fixture `behaviour/<name>`, read in place.
pub fn behaviour_fixture(name: &str) -> Value {
    let path = fixture_root().join("behaviour").join(name);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// The fixture's `gN:offset` cursor offset. Only the fixture's own cursors
/// are parsed; the provider's are compared, never read.
fn offset(cursor: &Value) -> usize {
    let text = cursor.as_str().expect("a fixture cursor");
    text.rsplit_once(':')
        .and_then(|(_, n)| n.parse().ok())
        .unwrap_or_else(|| panic!("fixture cursor {text:?}"))
}

/// The workload a fixture implies: the bytes its observations expect at
/// their offsets, padded to the seeded `output.produced_bytes`, ending
/// when the fixture expects a terminal state.
pub fn workload_for(fixture: &Value) -> Workload {
    let mut output = Vec::new();
    let mut exits = false;
    for step in fixture["steps"].as_array().unwrap() {
        let expect = &step["expect"]["document"];
        if let Some(state) = expect.get("state").and_then(Value::as_str) {
            exits |= state == "succeeded";
        }
        if let Some(data) = expect.get("data_b64").and_then(Value::as_str) {
            let data = STANDARD.decode(data).expect("fixture base64");
            let end = offset(&expect["next_cursor"]);
            let start = end - data.len();
            if output.len() < end {
                output.resize(end, b'.');
            }
            output[start..end].copy_from_slice(&data);
        }
    }
    if let Some(produced) = fixture["output"]["produced_bytes"].as_u64() {
        output.resize(usize::try_from(produced).unwrap(), b'.');
    }
    Workload { output, exits }
}

const SETTLE: Duration = Duration::from_secs(30);

async fn eventually<T>(what: &str, mut attempt: impl AsyncFnMut() -> Option<T>) -> T {
    let deadline = tokio::time::Instant::now() + SETTLE;
    loop {
        if let Some(value) = attempt().await {
            return value;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "{what}: not within {SETTLE:?}"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

async fn observe<P: SupportsOperations>(
    provider: &P,
    operation: &OperationRef,
    cursor: Option<&str>,
    max_bytes: usize,
) -> Result<ObservationPage, OperationError> {
    let request = ObserveRequest {
        cursor: cursor.map(str::to_owned),
        max_bytes,
        wait: Duration::ZERO,
    };
    provider.observe(operation, request).await
}

fn decoded(page: &ObservationPage) -> Vec<u8> {
    STANDARD
        .decode(page.data_b64.as_str())
        .expect("provider base64")
}

/// The page after `cursor` carrying exactly `len` bytes, under the smallest
/// page bound that holds them. The fixture fixes a page's bytes, not the
/// bound that produced them, and output grows with the bound.
async fn page_of<P: SupportsOperations>(
    provider: &P,
    operation: &OperationRef,
    cursor: &str,
    len: usize,
) -> Result<ObservationPage, OperationError> {
    let max = max_page(provider);
    let full = observe(provider, operation, Some(cursor), max).await?;
    if len == 0 || decoded(&full).len() <= len {
        return Ok(full);
    }
    let (mut lo, mut hi, mut best) = (1, max, full);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        match observe(provider, operation, Some(cursor), mid).await {
            Ok(page) if decoded(&page).len() >= len => {
                hi = mid;
                best = page;
            }
            _ => lo = mid + 1,
        }
    }
    Ok(best)
}

fn to_value<T: serde::Serialize>(value: T) -> Value {
    serde_json::to_value(value).expect("serialisable")
}

/// Run every step of `fixture` against `harness`, asserting what it asserts:
/// the issued ref, an error's `kind`, status `state` and `exit_code`, each
/// page's bytes, gap, cursors, state and `complete`, a result's derived
/// `success`, and the launch count after each step.
pub async fn drive<H: BehaviourHarness>(name: &str, fixture: &Value, harness: &H) {
    let provider = harness.provider();
    let first = &fixture["steps"][0];
    let workspace = match first["call"].as_str() {
        Some("start") => &first["expect"]["document"]["workspace"],
        _ => &first["args"]["ref"]["workspace"],
    };
    let workspace: WorkspaceRef =
        serde_json::from_value(workspace.clone()).expect("the first step names a workspace");
    let workload = workload_for(fixture);
    harness.program(&workload);
    let mut issued: Option<(Value, OperationRef)> = None;
    // Fixture cursor -> the provider's cursor at the same point.
    let mut cursors: BTreeMap<String, String> = BTreeMap::new();
    for (i, step) in fixture["steps"].as_array().unwrap().iter().enumerate() {
        let at = format!("{name}#{i} {}", step["call"]);
        let expect = &step["expect"]["document"];
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
                let marker = spec.launch_marker.as_ref().map(|m| format!("{}\n", m.line));
                let started = provider.start(&workspace, spec).await;
                if let (Ok(op), None) = (&started, expect.get("kind")) {
                    if let Some((_, first)) = &issued {
                        assert_eq!(op, first, "{at}: a second ref");
                    } else {
                        issued = Some((expect.clone(), op.clone()));
                        let origin = match marker {
                            Some(line) => read_marker(provider, op, &line, &at).await,
                            None => {
                                let page = observe(provider, op, None, max_page(provider)).await;
                                page.expect("the origin page").cursor.to_string()
                            }
                        };
                        cursors.insert("g1:0".into(), origin);
                        harness.release();
                    }
                }
                started.map(to_value)
            }
            "status" => {
                let op = reference();
                let want = expect.get("state").and_then(Value::as_str);
                let terminal = want.is_some_and(|s| s != "running");
                let status = provider.status(&op).await;
                match status {
                    Ok(s) if terminal && !s.state.is_terminal() => {
                        let s = eventually(&at, async || {
                            provider
                                .status(&op)
                                .await
                                .ok()
                                .filter(|s| s.state.is_terminal())
                        })
                        .await;
                        Ok(to_value(s))
                    }
                    other => other.map(to_value),
                }
            }
            "cancel" => provider
                .cancel(&reference(), Duration::from_millis(300))
                .await
                .map(to_value),
            "observe" => {
                let op = reference();
                let named = step["args"]["cursor"].as_str().unwrap();
                let cursor = cursors
                    .get(named)
                    .cloned()
                    .unwrap_or_else(|| named.to_owned());
                assert!(
                    !cursor.is_empty(),
                    "{at}: {named} names a position this provider's pages skip"
                );
                let len = expect
                    .get("data_b64")
                    .and_then(Value::as_str)
                    .map_or(0, |d| STANDARD.decode(d).unwrap().len());
                let lost = expect["gap"]["lost_bytes"].as_u64();
                if expect.get("kind").is_none() {
                    // Seeded output settles before it is read: the gap is
                    // exact only once every byte has been produced.
                    eventually(&at, async || {
                        let page = page_of(provider, &op, &cursor, len).await.ok()?;
                        let gap = page.gap.and_then(|g| g.lost_bytes).map(|n| n.get());
                        let ready = gap == lost && decoded(&page).len() >= len;
                        ready.then_some(())
                    })
                    .await;
                }
                let page = page_of(provider, &op, &cursor, len).await;
                if let Ok(page) = &page {
                    assert_eq!(page.cursor.as_str(), cursor, "{at}: cursor");
                    let data = decoded(page);
                    let want = STANDARD
                        .decode(expect["data_b64"].as_str().unwrap())
                        .unwrap();
                    // The page starts where the fixture's does. Its length is
                    // the provider's: base64 pads 10 bytes to the width of 12,
                    // so a provider filling its bound may carry more than a
                    // fixture page. Any more are the workload's next bytes.
                    let start = offset(&expect["next_cursor"]) - want.len();
                    let produced = &workload.output[start.min(workload.output.len())..];
                    assert_eq!(data[..want.len().min(data.len())], want[..], "{at}: data");
                    assert!(produced.starts_with(&data), "{at}: bytes after the data");
                    let next = expect["next_cursor"].as_str().unwrap().to_owned();
                    let ours = page.next_cursor.to_string();
                    if data.len() == want.len() {
                        let bound = cursors.entry(next).or_insert_with(|| ours.clone());
                        assert_eq!(*bound, ours, "{at}: next_cursor");
                    } else {
                        // Our cursor is past the fixture's; using it as the
                        // fixture's would skip bytes, so it stays unbound.
                        cursors.insert(next, String::new());
                    }
                    assert_eq!(
                        page.gap.and_then(|g| g.lost_bytes).map(|n| n.get()),
                        lost,
                        "{at}: gap"
                    );
                    assert_eq!(Value::Bool(page.complete), expect["complete"], "{at}");
                }
                page.map(to_value)
            }
            "result" => Ok(result(provider, &reference(), expect, &at).await),
            call => panic!("{at}: the driver does not dispatch {call:?}"),
        };
        match (expect.get("kind"), outcome) {
            (Some(kind), Err(error)) => assert_eq!(&to_value(error.kind), kind, "{at}"),
            (Some(kind), Ok(document)) => panic!("{at}: expected {kind}, got {document}"),
            (None, Err(error)) => panic!("{at}: {error:?}"),
            (None, Ok(document)) => {
                for key in ["workspace", "operation_key"] {
                    if let Some(want) = expect.get(key) {
                        assert_eq!(document.get(key), Some(want), "{at}: {key}");
                    }
                }
                for key in ["state", "exit_code", "success"] {
                    if let Some(want) = expect.get(key) {
                        let got = document.get(key).unwrap_or(&Value::Null);
                        assert_eq!(got, want, "{at}: {key}");
                    }
                }
            }
        }
        let want = step["launches"].as_u64().unwrap() as usize;
        eventually(&format!("{at}: launches"), async || {
            (harness.launches() >= want).then_some(())
        })
        .await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(harness.launches(), want, "{at}: launches");
    }
    provider.finalize(&workspace).await;
}

/// Read the launch marker the contract puts first on the stream; the cursor
/// after it is the fixture's `g1:0`.
async fn read_marker<P: SupportsOperations>(
    provider: &P,
    operation: &OperationRef,
    line: &str,
    at: &str,
) -> String {
    let origin = eventually(at, async || {
        observe(provider, operation, None, max_page(provider))
            .await
            .ok()
            .filter(|p| decoded(p).len() >= line.len())
    })
    .await;
    let page = page_of(provider, operation, origin.cursor.as_str(), line.len())
        .await
        .expect("the marker page");
    assert_eq!(decoded(&page), line.as_bytes(), "{at}: launch marker first");
    page.next_cursor.to_string()
}

fn max_page<P: SupportsOperations>(provider: &P) -> usize {
    usize::try_from(provider.describe().observation.max_page_bytes.get()).unwrap()
}

/// A result is assembled by the orchestrator, not the provider: it carries
/// the provider's terminal status as `process`, and the fixture's model,
/// environment, commits, artifacts, capture and finalization. Its `success`
/// is derived, and the opposite claim must be rejected.
async fn result<P: SupportsOperations>(
    provider: &P,
    operation: &OperationRef,
    expect: &Value,
    at: &str,
) -> Value {
    let process: OperationStatus = eventually(at, async || {
        provider
            .status(operation)
            .await
            .ok()
            .filter(|s| s.state.is_terminal())
    })
    .await;
    for key in ["state", "exit_code"] {
        assert_eq!(
            to_value(&process).get(key),
            expect["process"].get(key),
            "{at}: process {key}"
        );
    }
    let capture = serde_json::from_value(expect["capture"].clone()).expect("capture");
    let success = WorkspaceResult::success_of(process.state, &capture);
    let mut document = expect.clone();
    document["process"] = to_value(&process);
    document["success"] = Value::Bool(!success);
    assert!(
        serde_json::from_value::<WorkspaceResult>(document.clone()).is_err(),
        "{at}: success={} was accepted",
        !success
    );
    document["success"] = Value::Bool(success);
    let result: WorkspaceResult = serde_json::from_value(document).expect("a valid result");
    to_value(result)
}
