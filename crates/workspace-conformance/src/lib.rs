//! Shared black-box assertions for workspace provider implementations.

use agentic_workspace_core::{
    AgentRequest, CommandSpec, ContentRequest, LaunchManifest, OutputRequest, ResourceLimits,
    SecurityProfile, ToolPolicy, TranscriptRequest, WorkspaceError, WorkspaceProvider,
    WorkspaceRequest,
};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

pub fn minimal_manifest(
    execution_id: impl Into<String>,
    security_profile: SecurityProfile,
) -> LaunchManifest {
    LaunchManifest {
        schema: "apss.workspace-launch/v1".into(),
        execution_id: execution_id.into(),
        workspace: WorkspaceRequest {
            working_directory: "work".into(),
            security_profile,
            provider_hint: None,
        },
        content: ContentRequest::default(),
        agent: AgentRequest {
            harness: "conformance".into(),
            model: None,
            prompt: "run functional conformance".into(),
            arguments: vec![],
        },
        skills: vec![],
        tools: ToolPolicy::default(),
        capabilities: vec![],
        transcript: TranscriptRequest {
            session_id: "conformance-session".into(),
            source_format: "test".into(),
            destination: "artifacts/session.json".into(),
            required: false,
        },
        outputs: OutputRequest {
            artifacts: vec!["work/artifact.txt".into()],
            collect_logs: true,
        },
        limits: ResourceLimits {
            timeout_seconds: 30,
            memory_mb: None,
            cpu_millis: None,
            disk_mb: None,
        },
        metadata: BTreeMap::new(),
    }
}

pub fn assert_basic_lifecycle(
    provider: &impl WorkspaceProvider,
    manifest: &LaunchManifest,
    command: CommandSpec,
    expected_artifact: &str,
) -> Result<(), WorkspaceError> {
    let handle = provider.provision(manifest)?;
    let result = (|| {
        provider.hydrate(&handle, manifest)?;
        let execution = provider.execute(
            &handle,
            &command,
            Duration::from_secs(manifest.limits.timeout_seconds),
        )?;
        if execution.exit_code != Some(0) || execution.timed_out {
            return Err(WorkspaceError::Conformance(format!(
                "execution failed: status={:?}, timed_out={}, stderr={}",
                execution.exit_code,
                execution.timed_out,
                String::from_utf8_lossy(&execution.stderr)
            )));
        }
        let artifacts = provider.collect(&handle, &[PathBuf::from(expected_artifact)])?;
        if artifacts.len() != 1 || artifacts[0].bytes != b"conformant\n" {
            return Err(WorkspaceError::Conformance(
                "artifact did not round-trip".into(),
            ));
        }
        Ok(())
    })();
    let teardown = provider
        .destroy(&handle)
        .and_then(|()| provider.destroy(&handle));
    result.and(teardown)
}
