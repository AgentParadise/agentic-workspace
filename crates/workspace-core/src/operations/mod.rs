//! The APSS EXP-V1-0006 0.2.0 operation contract.
//!
//! `SupportsOperations` is separate from [`crate::WorkspaceProvider`] and does
//! not widen it, mirroring Python's optional `SupportsOperations` protocol: a
//! provider that can start, observe and cancel long-running operations
//! implements both. Output is never returned whole; callers read bounded
//! pages through opaque cursors.

mod contract;
mod spool;

use std::future::Future;
use std::time::Duration;

pub use contract::*;
pub use spool::OperationSpool;

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

/// Resolves a credential reference to its value inside the provider. The
/// value goes only where the binding says; no contract document carries it.
pub trait CredentialResolver: Send + Sync {
    fn resolve(&self, credential: &CredentialRef) -> Result<String, OperationError>;
}
