//! Serde models of the APSS EXP-V1-0006 0.2.0 operation contract.
//!
//! Every model rejects unknown fields, matching the schemas'
//! `additionalProperties: false`, and every invariant the schemas state is
//! checked during deserialisation, so a value of these types is a valid
//! document. Optional properties are `Option<T>` and reject an explicit
//! `null`; properties the schemas allow to be `null` are `Option<Option<T>>`,
//! so absent and `null` stay distinct, exactly as the schemas distinguish them.
//! The Python models in `agentic_isolation.contract` are the twin of this
//! module; both are checked against the same vendored fixtures.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use serde::{Deserialize, Deserializer, Serialize};

/// Deserialises an optional property that, when present, must not be `null`.
fn present<'de, D: Deserializer<'de>, T: Deserialize<'de>>(d: D) -> Result<Option<T>, D::Error> {
    T::deserialize(d).map(Some)
}

/// Deserialises an optional property that may be present as `null`.
fn nullable<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> Result<Option<Option<T>>, D::Error> {
    Option::<T>::deserialize(d).map(Some)
}

/// A string newtype whose invariant is checked on every construction.
macro_rules! checked_string {
    ($(#[$attr:meta])* $name:ident, $check:expr) => {
        $(#[$attr])*
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        pub struct $name(String);

        impl TryFrom<String> for $name {
            type Error = String;
            fn try_from(value: String) -> Result<Self, String> {
                let check: fn(&str) -> Result<(), String> = $check;
                check(&value).map(|()| Self(value))
            }
        }

        impl TryFrom<&str> for $name {
            type Error = String;
            fn try_from(value: &str) -> Result<Self, String> {
                Self::try_from(value.to_owned())
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> String {
                value.0
            }
        }

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

checked_string!(
    /// A string with at least one character.
    NonEmpty,
    |s| if s.is_empty() { Err("must not be empty".into()) } else { Ok(()) }
);

checked_string!(
    /// A non-empty path that does not start with `/`.
    RelativePath,
    |s| {
        if s.is_empty() || s.starts_with('/') {
            Err(format!("{s:?} is not a relative path"))
        } else {
            Ok(())
        }
    }
);

checked_string!(
    /// `sha256:` followed by 64 lowercase hex digits.
    Digest,
    |s| {
        let hex = s.strip_prefix("sha256:").unwrap_or("");
        if hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
            Ok(())
        } else {
            Err(format!("{s:?} is not a sha256 digest"))
        }
    }
);

checked_string!(
    /// Padded standard base64, as the observation schema's pattern accepts it.
    Base64,
    |s| {
        let body = s.trim_end_matches('=');
        let padding = s.len() - body.len();
        let alphabet = body.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/');
        if s.len() % 4 == 0 && padding <= 2 && alphabet {
            Ok(())
        } else {
            Err("data_b64 is not padded standard base64".into())
        }
    }
);

/// Whether a map key names a credential value, per
/// `credential-ref.schema.json#/$defs/credentialFreeStringMap`.
fn names_credential(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    key == "value"
        || [
            "secret",
            "token",
            "password",
            "passwd",
            "apikey",
            "api_key",
            "privatekey",
            "private_key",
            "credential",
        ]
        .iter()
        .any(|word| key.contains(word))
}

/// A string map that cannot carry a key naming a credential value (spec
/// 5.1.3). A secret under an innocuous key is forbidden normatively; no
/// schema can recognise it.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    try_from = "BTreeMap<String, String>",
    into = "BTreeMap<String, String>"
)]
pub struct CredentialFreeMap(BTreeMap<String, String>);

impl TryFrom<BTreeMap<String, String>> for CredentialFreeMap {
    type Error = String;
    fn try_from(map: BTreeMap<String, String>) -> Result<Self, String> {
        let named: Vec<&String> = map.keys().filter(|k| names_credential(k)).collect();
        if named.is_empty() {
            Ok(Self(map))
        } else {
            Err(format!(
                "keys {named:?} name a credential value; pass a CredentialRef binding instead"
            ))
        }
    }
}

impl From<CredentialFreeMap> for BTreeMap<String, String> {
    fn from(value: CredentialFreeMap) -> Self {
        value.0
    }
}

impl CredentialFreeMap {
    pub fn get(&self) -> &BTreeMap<String, String> {
        &self.0
    }
}

/// An argv with at least one element.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "Vec<String>", into = "Vec<String>")]
pub struct Argv(Vec<String>);

impl TryFrom<Vec<String>> for Argv {
    type Error = String;
    fn try_from(argv: Vec<String>) -> Result<Self, String> {
        if argv.is_empty() {
            Err("argv must have at least one element".into())
        } else {
            Ok(Self(argv))
        }
    }
}

impl From<Argv> for Vec<String> {
    fn from(value: Argv) -> Self {
        value.0
    }
}

impl Argv {
    pub fn as_slice(&self) -> &[String] {
        &self.0
    }
}

/// Names with `uniqueItems`, rejected rather than silently deduplicated.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "Vec<NonEmpty>", into = "Vec<NonEmpty>")]
pub struct UniqueNames(Vec<NonEmpty>);

impl TryFrom<Vec<NonEmpty>> for UniqueNames {
    type Error = String;
    fn try_from(names: Vec<NonEmpty>) -> Result<Self, String> {
        if names.iter().collect::<BTreeSet<_>>().len() == names.len() {
            Ok(Self(names))
        } else {
            Err("items must be unique".into())
        }
    }
}

impl From<UniqueNames> for Vec<NonEmpty> {
    fn from(value: UniqueNames) -> Self {
        value.0
    }
}

/// The `schema` discriminator of one document type.
macro_rules! schema_tag {
    ($name:ident, $tag:literal) => {
        #[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
        pub enum $name {
            #[default]
            #[serde(rename = $tag)]
            V1,
        }
    };
}

schema_tag!(CredentialRefSchema, "apss.credential-ref/v1");
schema_tag!(ContentRefSchema, "apss.content-ref/v1");
schema_tag!(EnvironmentRefSchema, "apss.environment-ref/v1");
schema_tag!(WorkspaceRefSchema, "apss.workspace-ref/v1");
schema_tag!(OperationSchema, "apss.workspace-operation/v1");
schema_tag!(StatusSchema, "apss.workspace-status/v1");
schema_tag!(ObservationSchema, "apss.workspace-observation/v1");
schema_tag!(ResultSchema, "apss.workspace-result/v1");
schema_tag!(FinalizationSchema, "apss.workspace-finalization/v1");
schema_tag!(ProviderSchema, "apss.workspace-provider/v1");

/// A model whose cross-field invariants are checked after field parsing: the
/// public struct deserialises through an unchecked twin and `check`.
macro_rules! checked_struct {
    (
        $(#[$attr:meta])*
        pub struct $name:ident via $unchecked:ident = $unchecked_lit:literal {
            $( $(#[$fattr:meta])* pub $field:ident : $ty:ty, )*
        }
        fn check($this:ident) -> Result<(), String> $check:block
    ) => {
        $(#[$attr])*
        #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
        #[serde(deny_unknown_fields, try_from = $unchecked_lit)]
        pub struct $name {
            $( $(#[$fattr])* pub $field: $ty, )*
        }

        #[doc(hidden)]
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        pub struct $unchecked {
            $( $(#[$fattr])* $field: $ty, )*
        }

        impl TryFrom<$unchecked> for $name {
            type Error = String;
            fn try_from(raw: $unchecked) -> Result<Self, String> {
                let $this = Self { $( $field: raw.$field, )* };
                $check
                Ok($this)
            }
        }
    };
}

/// `apss.credential-ref/v1`: a credential's name and scope, never its value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialRef {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<CredentialRefSchema>,
    pub name: NonEmpty,
    pub scope: NonEmpty,
}

/// How a resolved credential reaches the workspace.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialDelivery {
    Env,
    File,
}

/// Where the provider delivers a resolved credential inside the workspace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialBinding {
    #[serde(rename = "ref")]
    pub credential: CredentialRef,
    pub delivery: CredentialDelivery,
    pub target: NonEmpty,
}

/// `apss.content-ref/v1`: immutable content identified by digest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContentRef {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<ContentRefSchema>,
    pub digest: Digest,
    pub size_bytes: u64,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub media_type: Option<NonEmpty>,
    pub locator: NonEmpty,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub authority: Option<CredentialRef>,
}

/// `apss.environment-ref/v1`: opaque environment identity, compared by equality.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvironmentRef {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<EnvironmentRefSchema>,
    pub kind: NonEmpty,
    pub identifier: NonEmpty,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub digest: Option<NonEmpty>,
}

/// `apss.workspace-ref/v1`: an opaque provider-issued handle to one
/// workspace. It names no host path; only the issuing provider interprets
/// `resource_id`.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceRef {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<WorkspaceRefSchema>,
    pub provider: NonEmpty,
    pub resource_id: NonEmpty,
}

/// The line a launcher prints so a caller can find the operation's start.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchMarker {
    pub argv0: NonEmpty,
    pub line: NonEmpty,
}

/// `apss.workspace-operation/v1`: a request to start one operation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationSpec {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<OperationSchema>,
    pub execution_id: NonEmpty,
    pub operation_key: NonEmpty,
    pub argv: Argv,
    pub working_directory: RelativePath,
    pub environment: CredentialFreeMap,
    pub credential_bindings: Vec<CredentialBinding>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub launch_marker: Option<LaunchMarker>,
    pub timeout_seconds: std::num::NonZeroU64,
    pub metadata: CredentialFreeMap,
}

/// The reference a provider returns for a started operation.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationRef {
    pub workspace: WorkspaceRef,
    pub operation_id: NonEmpty,
    pub operation_key: NonEmpty,
}

/// Where an operation is in its life.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationState {
    Pending,
    Running,
    Succeeded,
    Failed,
    Cancelled,
    TimedOut,
    Lost,
}

impl OperationState {
    pub fn is_terminal(self) -> bool {
        !matches!(self, Self::Pending | Self::Running)
    }
}

/// The closed set of operation failures. Callers dispatch on this, never on
/// a message.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    RejectedPolicy,
    UnsupportedCapability,
    HydrationFailed,
    Conflict,
    UnknownOperation,
    Timeout,
    Cancelled,
    ResourceLost,
    ProviderUnavailable,
    Internal,
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = serde_json::to_value(self).map_err(|_| fmt::Error)?;
        f.write_str(value.as_str().unwrap_or("internal"))
    }
}

/// A typed operation failure, `apss.workspace-status/v1` in its error form.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, thiserror::Error)]
#[serde(deny_unknown_fields)]
#[error("{kind}: {message}")]
pub struct OperationError {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<StatusSchema>,
    pub kind: ErrorKind,
    pub message: NonEmpty,
    #[serde(
        rename = "ref",
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub operation: Option<OperationRef>,
}

impl OperationError {
    /// An error with a message; an empty message becomes the kind's name.
    pub fn new(
        kind: ErrorKind,
        message: impl Into<String>,
        operation: Option<OperationRef>,
    ) -> Self {
        let message =
            NonEmpty::try_from(message.into()).unwrap_or_else(|_| NonEmpty(kind.to_string()));
        Self {
            schema: Some(StatusSchema::V1),
            kind,
            message,
            operation,
        }
    }
}

checked_struct! {
    /// `apss.workspace-status/v1`: where an operation is. Exit facts exist
    /// only once it has ended.
    pub struct OperationStatus via UncheckedOperationStatus = "UncheckedOperationStatus" {
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub schema: Option<StatusSchema>,
        #[serde(rename = "ref")]
        pub operation: OperationRef,
        pub state: OperationState,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "nullable")]
        pub exit_code: Option<Option<i64>>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "nullable")]
        pub signal: Option<Option<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub started_at: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub ended_at: Option<String>,
        pub execution_id: NonEmpty,
        pub metadata: CredentialFreeMap,
    }
    fn check(this) -> Result<(), String> {
        let exit_facts = this.exit_code.is_some() || this.signal.is_some() || this.ended_at.is_some();
        if !this.state.is_terminal() && exit_facts {
            return Err(format!("a {:?} operation cannot carry exit facts", this.state));
        }
    }
}

/// A `workspace-status.schema.json` document: a status or a typed error,
/// never both.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StatusDocument {
    Status(OperationStatus),
    Error(OperationError),
}

/// Bytes lost before a page. `None` means the extent is unknown.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationGap {
    // Required but nullable: `deserialize_with` stops serde defaulting a
    // missing `Option` field to `None`.
    #[serde(deserialize_with = "Option::deserialize")]
    pub lost_bytes: Option<std::num::NonZeroU64>,
}

checked_struct! {
    /// `apss.workspace-observation/v1`: one bounded page addressed by opaque
    /// cursors. A complete page has a terminal state.
    pub struct ObservationPage via UncheckedObservationPage = "UncheckedObservationPage" {
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub schema: Option<ObservationSchema>,
        #[serde(rename = "ref")]
        pub operation: OperationRef,
        pub state: OperationState,
        pub cursor: NonEmpty,
        pub next_cursor: NonEmpty,
        pub data_b64: Base64,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub gap: Option<ObservationGap>,
        pub complete: bool,
    }
    fn check(this) -> Result<(), String> {
        if this.complete && !this.state.is_terminal() {
            return Err(format!("a complete page cannot have state {:?}", this.state));
        }
    }
}

/// When a finalization step ran.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinalizationStage {
    WhileRunning,
    BeforeDelete,
}

/// The outcome of finalizing a workspace.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FinalizationState {
    Completed,
    Retained,
    Failed,
}

checked_struct! {
    /// `apss.workspace-finalization/v1`: completed carries nothing else,
    /// retained names the kept workspace, failed says why.
    pub struct FinalizationOutcome via UncheckedFinalizationOutcome = "UncheckedFinalizationOutcome" {
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub schema: Option<FinalizationSchema>,
        pub state: FinalizationState,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub stage: Option<FinalizationStage>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub error: Option<OperationError>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub retained: Option<WorkspaceRef>,
    }
    fn check(this) -> Result<(), String> {
        let shape = (this.stage, this.error.is_some(), this.retained.is_some());
        let fits = match this.state {
            FinalizationState::Completed => matches!(shape, (None, false, false)),
            FinalizationState::Retained => {
                matches!(shape, (Some(FinalizationStage::BeforeDelete), true, true))
            }
            FinalizationState::Failed => matches!(shape, (Some(_), true, false)),
        };
        if !fits {
            return Err(format!("fields do not match a {:?} finalization", this.state));
        }
    }
}

/// The commit a workspace destination resolved to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolvedCommit {
    pub destination: RelativePath,
    pub commit: NonEmpty,
}

/// Whether transcript capture completed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaptureState {
    Captured,
    Incomplete,
    Failed,
    Unknown,
    Disabled,
}

/// The execution's transcript capture outcome.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaptureOutcome {
    pub required: bool,
    pub state: CaptureState,
    pub needs_backfill: bool,
}

checked_struct! {
    /// `apss.workspace-result/v1`. `success` is derived from process and
    /// capture and must agree with them; it is never asserted alone.
    pub struct WorkspaceResult via UncheckedWorkspaceResult = "UncheckedWorkspaceResult" {
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub schema: Option<ResultSchema>,
        pub execution_id: NonEmpty,
        pub metadata: CredentialFreeMap,
        pub process: OperationStatus,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "nullable")]
        pub effective_model: Option<Option<String>>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub effective_environment: Option<EnvironmentRef>,
        pub resolved_commits: Vec<ResolvedCommit>,
        pub artifacts: Vec<ContentRef>,
        pub capture: CaptureOutcome,
        pub finalization: FinalizationOutcome,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub primary_failure: Option<OperationError>,
        #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "present")]
        pub teardown_failure: Option<OperationError>,
        pub success: bool,
    }
    fn check(this) -> Result<(), String> {
        if !this.process.state.is_terminal() {
            return Err("a result's process must be terminal".into());
        }
        let succeeded = this.process.state == OperationState::Succeeded;
        let captured = !this.capture.required || this.capture.state == CaptureState::Captured;
        if this.success != (succeeded && captured) {
            return Err(format!(
                "success={} contradicts process {:?} and capture {:?}",
                this.success, this.process.state, this.capture
            ));
        }
        if succeeded && this.primary_failure.is_some() {
            return Err("a succeeded process cannot carry a primary_failure".into());
        }
    }
}

/// Which resource limits the provider can actually enforce.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnforceableLimits {
    pub cpu: bool,
    pub memory: bool,
    pub disk: bool,
    pub network: bool,
    pub timeout: bool,
}

/// How completely a provider's retained output matches what was produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationIntegrity {
    Exact,
    ContinuityDetected,
}

/// How much output the provider retains and the largest page it serves.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationDescriptor {
    pub retention_bytes: std::num::NonZeroU64,
    pub max_page_bytes: std::num::NonZeroU64,
    pub integrity: ObservationIntegrity,
}

/// The security profile a provider runs workloads under.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SecurityProfile {
    InsecureLocal,
    Isolated,
}

/// `apss.workspace-provider/v1`: what a provider can enforce, published
/// before launch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderDescriptor {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "present"
    )]
    pub schema: Option<ProviderSchema>,
    pub provider: NonEmpty,
    pub security_profile: SecurityProfile,
    pub enforceable_limits: EnforceableLimits,
    pub capabilities: UniqueNames,
    pub observation: ObservationDescriptor,
    pub interactive: bool,
    pub environment_kinds: UniqueNames,
}
