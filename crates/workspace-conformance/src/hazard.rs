//! APSS EXP-V1-0006 0.2.1 lifecycle-hazard fixtures (LH-1..LH-7) for Rust providers.
//!
//! The provider-neutral half of the hazard harness: it loads the vendored
//! `behaviour/hazard-*.json` fixtures and judges one step's measured outcome
//! against them. A driver performs the call, establishes the step's `given`
//! conditions, and measures `descendant_alive` itself (see [`live_processes`]);
//! nothing here takes liveness from the fixture.
//!
//! The Rust operations trait (`SupportsOperations`) lands with PR #60, so no
//! Rust provider is driven through these fixtures yet. That driver belongs in
//! #60's `behaviour::drive`, calling [`judge`] per step. The Python harness
//! (`lib/python/agentic_isolation/tests/test_behaviour_hazards.py`) drives
//! both Python providers today and uses the same matching rule.

use serde::Deserialize;
use serde_json::Value;
use std::fmt;
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

const OBSERVATION_FIELDS: [&str; 4] = ["state", "complete", "gap", "loss_detected"];

/// Whether `measured` is one of `step`'s correct responses, judged by outcome
/// (state, error kind and ref, page fields), never by message prose.
pub fn judge(step: &HazardStep, measured: &Measured) -> Result<(), Mismatch> {
    if let Some(bound) = step.settles_within_seconds {
        if measured.elapsed.as_secs_f64() > bound {
            return Err(Mismatch::Unsettled {
                bound,
                elapsed: measured.elapsed,
            });
        }
    }
    if !step.alternatives().iter().any(|alt| matches(alt, measured)) {
        return Err(Mismatch::NoAlternative {
            measured: Box::new(measured.clone()),
        });
    }
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
    Ok(())
}

fn matches(alternative: &Expectation, measured: &Measured) -> bool {
    if alternative
        .descendant_alive
        .is_some_and(|alive| alive != measured.descendant_alive)
    {
        return false;
    }
    let want = &alternative.document;
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

/// Live, non-zombie pids whose environment carries `variable=token`: the
/// measured `descendant_alive`, detached descendants included. Linux only.
pub fn live_processes(variable: &str, token: &str) -> Vec<u32> {
    let needle = format!("{variable}={token}");
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return Vec::new();
    };
    let me = std::process::id();
    entries
        .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse::<u32>().ok())
        .filter(|pid| *pid != me)
        .filter(|pid| {
            let environ = std::fs::read(format!("/proc/{pid}/environ")).unwrap_or_default();
            let status = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
            environ
                .split(|b| *b == 0)
                .any(|var| var == needle.as_bytes())
                && !status.contains("\nState:\tZ")
        })
        .collect()
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
        assert_eq!(judge(step, &measured(cancelled(), false)), Ok(()));
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
        assert_eq!(judge(step, &measured(timeout(true), true)), Ok(()));
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
        assert_eq!(judge(step, &clean), Ok(()));
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
        assert_eq!(judge(step, &measured(page(true), false)), Ok(()));
        assert!(judge(step, &measured(page(false), false)).is_err());
    }

    #[test]
    fn live_processes_measures_a_tokened_child_and_not_after_it_exits() {
        let token = format!("rust-hazard-{}", std::process::id());
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .env("AW_HARNESS_RUN", &token)
            .spawn()
            .unwrap();
        assert_eq!(live_processes("AW_HARNESS_RUN", &token), [child.id()]);
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(live_processes("AW_HARNESS_RUN", &token).is_empty());
    }
}
