//! Provider-neutral workspace lifecycle contracts.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;
use thiserror::Error;
use wait_timeout::ChildExt;

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
        for repository in &self.content.repositories {
            validate_relative_path(&repository.destination)?;
        }
        for input in self
            .content
            .inputs
            .iter()
            .chain(self.content.context_files.iter())
        {
            validate_relative_path(&input.destination)?;
        }
        for artifact in &self.outputs.artifacts {
            validate_relative_path(artifact)?;
        }
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
        reject_duplicates(
            self.skills.iter().map(|skill| skill.name.as_str()),
            "skill names",
        )?;
        reject_duplicates(self.capabilities.iter().map(String::as_str), "capabilities")?;
        reject_duplicates(self.tools.allow.iter().map(String::as_str), "allowed tools")?;
        reject_duplicates(self.tools.deny.iter().map(String::as_str), "denied tools")?;
        if self
            .tools
            .allow
            .iter()
            .any(|tool| self.tools.deny.contains(tool))
        {
            return Err(WorkspaceError::InvalidManifest(
                "a tool cannot be both allowed and denied".into(),
            ));
        }
        Ok(())
    }
}

fn reject_duplicates<'a>(
    values: impl Iterator<Item = &'a str>,
    label: &str,
) -> Result<(), WorkspaceError> {
    let mut seen = std::collections::BTreeSet::new();
    if values.into_iter().all(|value| seen.insert(value)) {
        Ok(())
    } else {
        Err(WorkspaceError::InvalidManifest(format!(
            "{label} must be unique"
        )))
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

pub fn materialize_file(root: &std::path::Path, input: &FileInput) -> Result<(), WorkspaceError> {
    validate_relative_path(&input.destination)?;
    let canonical_root = std::fs::canonicalize(root).map_err(|source| WorkspaceError::Io {
        path: root.to_path_buf(),
        source,
    })?;
    let destination = root.join(&input.destination);
    if let Some(parent) = destination.parent() {
        std::fs::create_dir_all(parent).map_err(|source| WorkspaceError::Io {
            path: parent.to_path_buf(),
            source,
        })?;
        let canonical_parent =
            std::fs::canonicalize(parent).map_err(|source| WorkspaceError::Io {
                path: parent.to_path_buf(),
                source,
            })?;
        if !canonical_parent.starts_with(&canonical_root) {
            return Err(WorkspaceError::InvalidHandle(canonical_parent));
        }
    }
    let source_path = std::path::Path::new(&input.source);
    let bytes = std::fs::read(source_path).map_err(|source| WorkspaceError::Io {
        path: source_path.to_path_buf(),
        source,
    })?;
    if let Some(expected) = &input.sha256 {
        let actual = format!("{:x}", Sha256::digest(&bytes));
        if !actual.eq_ignore_ascii_case(expected) {
            return Err(WorkspaceError::DigestMismatch {
                path: source_path.to_path_buf(),
                expected: expected.clone(),
                actual,
            });
        }
    }
    std::fs::write(&destination, bytes).map_err(|source| WorkspaceError::Io {
        path: destination.clone(),
        source,
    })?;
    if input.read_only {
        let mut permissions = std::fs::metadata(&destination)
            .map_err(|source| WorkspaceError::Io {
                path: destination.clone(),
                source,
            })?
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(&destination, permissions).map_err(|source| {
            WorkspaceError::Io {
                path: destination,
                source,
            }
        })?;
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceHandle {
    pub id: String,
    pub root: PathBuf,
    pub working_directory: PathBuf,
    pub provider_reference: Option<String>,
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

pub fn execute_process(
    command: &mut Command,
    timeout: Duration,
    context: PathBuf,
) -> Result<ExecutionResult, WorkspaceError> {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|source| WorkspaceError::Io {
            path: context.clone(),
            source,
        })?;
    let mut stdout = child.stdout.take().expect("stdout configured as piped");
    let mut stderr = child.stderr.take().expect("stderr configured as piped");
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes)
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.read_to_end(&mut bytes).map(|_| bytes)
    });

    let status = child
        .wait_timeout(timeout)
        .map_err(|source| WorkspaceError::Io {
            path: context.clone(),
            source,
        })?;
    let timed_out = status.is_none();
    let status = match status {
        Some(status) => status,
        None => {
            child.kill().map_err(|source| WorkspaceError::Io {
                path: context.clone(),
                source,
            })?;
            child.wait().map_err(|source| WorkspaceError::Io {
                path: context.clone(),
                source,
            })?
        }
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| WorkspaceError::ProcessReader("stdout"))?
        .map_err(|source| WorkspaceError::Io {
            path: context.clone(),
            source,
        })?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| WorkspaceError::ProcessReader("stderr"))?
        .map_err(|source| WorkspaceError::Io {
            path: context,
            source,
        })?;
    Ok(ExecutionResult {
        exit_code: status.code(),
        stdout,
        stderr,
        timed_out,
    })
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
    #[error("{0} process reader panicked")]
    ProcessReader(&'static str),
    #[error("provider command {operation} failed: {stderr}")]
    ProviderCommand { operation: String, stderr: String },
    #[error("digest mismatch for {path}: expected {expected}, got {actual}")]
    DigestMismatch {
        path: PathBuf,
        expected: String,
        actual: String,
    },
    #[error("conformance failure: {0}")]
    Conformance(String),
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}
