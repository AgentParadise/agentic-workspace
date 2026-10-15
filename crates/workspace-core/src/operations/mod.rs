//! The APSS EXP-V1-0006 0.2.0 operation contract.
//!
//! `SupportsOperations` is separate from [`crate::WorkspaceProvider`] and does
//! not widen it, mirroring Python's optional `SupportsOperations` protocol: a
//! provider that can start, observe and cancel long-running operations
//! implements both. Output is never returned whole; callers read bounded
//! pages through opaque cursors.

mod contract;
mod spool;

use std::collections::BTreeSet;
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

pub use contract::*;
pub use spool::OperationSpool;

/// Credential values a caller registers when it starts an operation. The
/// provider replaces each with `[REDACTED]` before the output is stored, so
/// no page or final output carries them. A set: order, duplicates and empty
/// values do not matter, and `Debug` never prints a value.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct Redaction(BTreeSet<Vec<u8>>);

impl Redaction {
    pub fn new<V: Into<Vec<u8>>>(values: impl IntoIterator<Item = V>) -> Self {
        Self(
            values
                .into_iter()
                .map(Into::into)
                .filter(|v| !v.is_empty())
                .collect(),
        )
    }

    /// No values: output is stored exactly as printed.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn values(&self) -> impl Iterator<Item = &[u8]> {
        self.0.iter().map(Vec::as_slice)
    }
}

impl std::fmt::Debug for Redaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Redaction({} values)", self.0.len())
    }
}

/// One `observe` request: where to resume, the largest serialised page the
/// caller accepts, and how long to wait for new output when there is none.
#[derive(Debug, Clone, Default)]
pub struct ObserveRequest {
    /// `None` reads from the start of the operation's output.
    pub cursor: Option<String>,
    /// Clamped to the provider's published `max_page_bytes`.
    pub max_bytes: usize,
    pub wait: Duration,
}

/// Turns a [`CredentialRef`] into its secret value at launch, injected into a
/// provider so specs, refs and pages only ever carry the reference.
pub trait CredentialResolver: Send + Sync {
    fn resolve<'a>(
        &'a self,
        credential: &'a CredentialRef,
    ) -> Pin<Box<dyn Future<Output = Result<String, OperationError>> + Send + 'a>>;
}

/// Start, observe, cancel and finalize operations in a provider's workspaces.
///
/// Every failure is a typed [`OperationError`]; callers dispatch on its kind.
pub trait SupportsOperations: Send + Sync {
    /// What the provider can enforce and how it serves output.
    fn describe(&self) -> ProviderDescriptor;

    /// Start `spec` once per `(workspace, operation_key)`. A same-key start
    /// with an equal spec returns the original ref and launches nothing; a
    /// different spec is `conflict`.
    fn start(
        &self,
        workspace: &WorkspaceRef,
        spec: OperationSpec,
    ) -> impl Future<Output = Result<OperationRef, OperationError>> + Send;

    /// [`SupportsOperations::start`], registering `redaction`: those values,
    /// and any the provider resolves from `spec`, never reach the operation's
    /// stored output or pages. A same-key replay must register an equal set,
    /// or it is `conflict` naming no value. The default serves only an empty
    /// set and refuses any other as `unsupported_capability`, so a provider
    /// that cannot redact never claims it did.
    fn start_redacted(
        &self,
        workspace: &WorkspaceRef,
        spec: OperationSpec,
        redaction: Redaction,
    ) -> impl Future<Output = Result<OperationRef, OperationError>> + Send {
        async move {
            if redaction.is_empty() {
                return self.start(workspace, spec).await;
            }
            Err(OperationError::new(
                ErrorKind::UnsupportedCapability,
                "this provider cannot redact registered values from operation output",
                None,
            ))
        }
    }

    /// The operation's state, from a channel the workload cannot write.
    fn status(
        &self,
        operation: &OperationRef,
    ) -> impl Future<Output = Result<OperationStatus, OperationError>> + Send;

    /// One bounded page of output after `request.cursor`.
    fn observe(
        &self,
        operation: &OperationRef,
        request: ObserveRequest,
    ) -> impl Future<Output = Result<ObservationPage, OperationError>> + Send;

    /// End the operation's whole process tree: TERM, then KILL after `grace`.
    /// Idempotent: a terminal operation returns its status unchanged.
    fn cancel(
        &self,
        operation: &OperationRef,
        grace: Duration,
    ) -> impl Future<Output = Result<OperationStatus, OperationError>> + Send;

    /// End every operation still running in `workspace` and drop the
    /// provider's records of it, reporting how teardown went.
    fn finalize(
        &self,
        workspace: &WorkspaceRef,
    ) -> impl Future<Output = FinalizationOutcome> + Send;
}
