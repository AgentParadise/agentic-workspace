//! Provider-neutral workspace lifecycle contracts.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;
use thiserror::Error;

pub const MANIFEST_SCHEMA: &str = "apss.workspace-launch/v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchManifest {
    pub schema: String,
    pub execution_id: String,
    pub workspace: WorkspaceRequest,
    #[serde(default)]
    pub content: ContentRequest,
    pub agent: AgentRequest,
    #[serde(default)]
    pub skills: Vec<SkillRequest>,
    #[serde(default)]
    pub tools: ToolPolicy,
    #[serde(default)]
    pub capabilities: Vec<String>,
    pub transcript: TranscriptRequest,
    #[serde(default)]
    pub outputs: OutputRequest,
    #[serde(default)]
    pub limits: ResourceLimits,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceRequest {
    pub working_directory: String,
    pub security_profile: SecurityProfile,
    #[serde(default)]
    pub provider_hint: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SecurityProfile {
    InsecureLocal,
    Isolated,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContentRequest {
    #[serde(default)]
    pub repositories: Vec<RepositoryRequest>,
    #[serde(default)]
    pub inputs: Vec<FileInput>,
    #[serde(default)]
    pub context_files: Vec<FileInput>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RepositoryRequest {
    pub url: String,
    pub revision: String,
    pub destination: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileInput {
    pub source: String,
    pub destination: String,
    #[serde(default)]
    pub read_only: bool,
    #[serde(default)]
    pub sha256: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentRequest {
    pub harness: String,
    #[serde(default)]
    pub model: Option<String>,
    pub prompt: String,
    #[serde(default)]
    pub arguments: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillRequest {
    pub name: String,
    pub source: String,
    pub revision: String,
    pub digest: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolPolicy {
    #[serde(default)]
    pub allow: Vec<String>,
    #[serde(default)]
    pub deny: Vec<String>,
    #[serde(default)]
    pub sandbox: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TranscriptRequest {
    pub session_id: String,
    pub source_format: String,
    pub destination: String,
    #[serde(default)]
    pub required: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputRequest {
    #[serde(default)]
    pub artifacts: Vec<String>,
    #[serde(default)]
    pub collect_logs: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResourceLimits {
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    #[serde(default)]
    pub memory_mb: Option<u64>,
    #[serde(default)]
    pub cpu_millis: Option<u32>,
    #[serde(default)]
    pub disk_mb: Option<u64>,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            timeout_seconds: default_timeout(),
            memory_mb: None,
            cpu_millis: None,
            disk_mb: None,
        }
    }
}

const fn default_timeout() -> u64 {
    3600
}

impl LaunchManifest {
    pub fn validate_boundary(&self) -> Result<(), WorkspaceError> {
        if self.schema != MANIFEST_SCHEMA {
            return Err(WorkspaceError::InvalidManifest(format!(
                "unsupported schema {}",
                self.schema
            )));
        }
        validate_identifier(&self.execution_id)?;
        validate_relative_path(&self.workspace.working_directory)?;
        validate_relative_path(&self.transcript.destination)?;
        if self.execution_id.trim().is_empty()
            || self.agent.harness.trim().is_empty()
            || self.agent.prompt.trim().is_empty()
        {
            return Err(WorkspaceError::InvalidManifest(
                "execution_id, agent.harness, and agent.prompt are required".into(),
            ));
        }
        if self.limits.timeout_seconds == 0 {
            return Err(WorkspaceError::InvalidManifest(
                "timeout_seconds must be greater than zero".into(),
            ));
        }
        Ok(())
    }
}

pub fn validate_identifier(value: &str) -> Result<(), WorkspaceError> {
    let path = std::path::Path::new(value);
    if !value.is_empty()
        && path.components().count() == 1
        && matches!(
            path.components().next(),
            Some(std::path::Component::Normal(_))
        )
    {
        Ok(())
    } else {
        Err(WorkspaceError::UnsafePath(value.into()))
    }
}

pub fn validate_relative_path(path: &str) -> Result<(), WorkspaceError> {
    let path = std::path::Path::new(path);
    let safe = !path.as_os_str().is_empty()
        && !path.is_absolute()
        && path.components().all(|component| {
            matches!(
                component,
                std::path::Component::Normal(_) | std::path::Component::CurDir
            )
        });
    if safe {
        Ok(())
    } else {
        Err(WorkspaceError::UnsafePath(path.display().to_string()))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceHandle {
    pub id: String,
    pub root: PathBuf,
    pub working_directory: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    pub program: String,
    pub arguments: Vec<String>,
    pub environment: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecutionResult {
    pub exit_code: Option<i32>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub timed_out: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Artifact {
    pub relative_path: PathBuf,
    pub bytes: Vec<u8>,
}

pub trait WorkspaceProvider {
    fn name(&self) -> &'static str;
    fn security_profile(&self) -> SecurityProfile;
    fn provision(&self, manifest: &LaunchManifest) -> Result<WorkspaceHandle, WorkspaceError>;
    fn hydrate(
        &self,
        handle: &WorkspaceHandle,
        manifest: &LaunchManifest,
    ) -> Result<(), WorkspaceError>;
    fn execute(
        &self,
        handle: &WorkspaceHandle,
        command: &CommandSpec,
        timeout: Duration,
    ) -> Result<ExecutionResult, WorkspaceError>;
    fn collect(
        &self,
        handle: &WorkspaceHandle,
        relative_paths: &[PathBuf],
    ) -> Result<Vec<Artifact>, WorkspaceError>;
    fn destroy(&self, handle: &WorkspaceHandle) -> Result<(), WorkspaceError>;
}

#[derive(Debug, Error)]
pub enum WorkspaceError {
    #[error("invalid launch manifest: {0}")]
    InvalidManifest(String),
    #[error("unsafe workspace path: {0}")]
    UnsafePath(String),
    #[error("insecure Local provider is not enabled")]
    LocalNotEnabled,
    #[error("insecure Local provider is forbidden in production")]
    LocalForbiddenInProduction,
    #[error("workspace handle is outside the configured provider root: {0}")]
    InvalidHandle(PathBuf),
    #[error("unsupported operation: {0}")]
    Unsupported(String),
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}
