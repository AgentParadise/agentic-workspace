//! Explicitly insecure local filesystem workspace provider.

use agentic_workspace_core::{
    Artifact, CommandSpec, ExecutionResult, LaunchManifest, SecurityProfile, WorkspaceError,
    WorkspaceHandle, WorkspaceProvider, execute_process, materialize_file, validate_identifier,
    validate_relative_path,
};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    Test,
    Development,
    Production,
}

#[derive(Debug, Clone)]
pub struct LocalProvider {
    root: PathBuf,
}

impl LocalProvider {
    pub fn enable_insecure(
        root: impl Into<PathBuf>,
        mode: RuntimeMode,
        explicit_opt_in: bool,
    ) -> Result<Self, WorkspaceError> {
        if mode == RuntimeMode::Production {
            return Err(WorkspaceError::LocalForbiddenInProduction);
        }
        if !explicit_opt_in {
            return Err(WorkspaceError::LocalNotEnabled);
        }
        let root = root.into();
        fs::create_dir_all(&root).map_err(|error| Self::io(&root, error))?;
        let root = fs::canonicalize(&root).map_err(|error| Self::io(&root, error))?;
        Ok(Self { root })
    }

    fn io(path: &Path, source: std::io::Error) -> WorkspaceError {
        WorkspaceError::Io {
            path: path.to_path_buf(),
            source,
        }
    }

    fn validate_handle(&self, handle: &WorkspaceHandle) -> Result<(), WorkspaceError> {
        validate_identifier(&handle.id)?;
        if handle.root.parent() != Some(self.root.as_path())
            || handle.root.file_name() != Some(handle.id.as_ref())
            || !handle.working_directory.starts_with(&handle.root)
        {
            return Err(WorkspaceError::InvalidHandle(handle.root.clone()));
        }
        Ok(())
    }

    fn validate_existing_path(&self, path: &Path) -> Result<PathBuf, WorkspaceError> {
        let canonical = fs::canonicalize(path).map_err(|error| Self::io(path, error))?;
        if !canonical.starts_with(&self.root) {
            return Err(WorkspaceError::InvalidHandle(canonical));
        }
        Ok(canonical)
    }
}

impl WorkspaceProvider for LocalProvider {
    fn name(&self) -> &'static str {
        "local"
    }

    fn security_profile(&self) -> SecurityProfile {
        SecurityProfile::InsecureLocal
    }

    fn provision(&self, manifest: &LaunchManifest) -> Result<WorkspaceHandle, WorkspaceError> {
        manifest.validate_boundary()?;
        if manifest.workspace.security_profile != SecurityProfile::InsecureLocal {
            return Err(WorkspaceError::InvalidManifest(
                "Local requires security_profile=insecure-local".into(),
            ));
        }
        let root = self.root.join(&manifest.execution_id);
        let working_directory = root.join(&manifest.workspace.working_directory);
        fs::create_dir_all(&working_directory)
            .map_err(|error| Self::io(&working_directory, error))?;
        Ok(WorkspaceHandle {
            id: manifest.execution_id.clone(),
            root,
            working_directory,
            provider_reference: None,
        })
    }

    fn hydrate(
        &self,
        handle: &WorkspaceHandle,
        manifest: &LaunchManifest,
    ) -> Result<(), WorkspaceError> {
        self.validate_handle(handle)?;
        if !manifest.content.repositories.is_empty() {
            return Err(WorkspaceError::Unsupported(
                "Local repository cloning is not implemented; provide a pre-hydrated directory"
                    .into(),
            ));
        }
        for input in manifest
            .content
            .inputs
            .iter()
            .chain(manifest.content.context_files.iter())
        {
            materialize_file(&handle.root, input)?;
        }
        Ok(())
    }

    fn execute(
        &self,
        handle: &WorkspaceHandle,
        command: &CommandSpec,
        timeout: Duration,
    ) -> Result<ExecutionResult, WorkspaceError> {
        self.validate_handle(handle)?;
        self.validate_existing_path(&handle.working_directory)?;
        let mut process = Command::new(&command.program);
        process
            .args(&command.arguments)
            .envs(&command.environment)
            .current_dir(&handle.working_directory);
        execute_process(&mut process, timeout, handle.working_directory.clone())
    }

    fn collect(
        &self,
        handle: &WorkspaceHandle,
        relative_paths: &[PathBuf],
    ) -> Result<Vec<Artifact>, WorkspaceError> {
        self.validate_handle(handle)?;
        let mut artifacts = Vec::new();
        for relative_path in relative_paths {
            validate_relative_path(&relative_path.to_string_lossy())?;
            let path = handle.root.join(relative_path);
            let path = self.validate_existing_path(&path)?;
            let bytes = fs::read(&path).map_err(|error| Self::io(&path, error))?;
            artifacts.push(Artifact {
                relative_path: relative_path.clone(),
                bytes,
            });
        }
        Ok(artifacts)
    }

    fn destroy(&self, handle: &WorkspaceHandle) -> Result<(), WorkspaceError> {
        self.validate_handle(handle)?;
        if !handle.root.exists() {
            return Ok(());
        }
        let root = self.validate_existing_path(&handle.root)?;
        fs::remove_dir_all(&root).map_err(|error| Self::io(&root, error))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentic_workspace_core::{
        AgentRequest, ContentRequest, OutputRequest, ResourceLimits, ToolPolicy, TranscriptRequest,
        WorkspaceRequest,
    };
    use std::collections::BTreeMap;

    fn manifest() -> LaunchManifest {
        LaunchManifest {
            schema: "apss.workspace-launch/v1".into(),
            execution_id: "local-test".into(),
            workspace: WorkspaceRequest {
                working_directory: "repos/example".into(),
                security_profile: SecurityProfile::InsecureLocal,
                provider_hint: Some("local".into()),
            },
            content: ContentRequest::default(),
            agent: AgentRequest {
                harness: "test".into(),
                model: None,
                prompt: "test".into(),
                arguments: vec![],
            },
            skills: vec![],
            tools: ToolPolicy::default(),
            capabilities: vec![],
            transcript: TranscriptRequest {
                session_id: "session-local".into(),
                source_format: "test".into(),
                destination: "artifacts/output/session.json".into(),
                required: false,
            },
            outputs: OutputRequest::default(),
            limits: ResourceLimits::default(),
            metadata: BTreeMap::new(),
        }
    }

    #[test]
    fn local_requires_explicit_non_production_enablement() {
        let root = tempfile::tempdir().unwrap();
        assert!(matches!(
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Test, false),
            Err(WorkspaceError::LocalNotEnabled)
        ));
        assert!(matches!(
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Production, true),
            Err(WorkspaceError::LocalForbiddenInProduction)
        ));
    }

    #[test]
    fn lifecycle_is_idempotent_and_collects_artifacts() {
        let root = tempfile::tempdir().unwrap();
        let provider =
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Test, true).unwrap();
        let manifest = manifest();
        let handle = provider.provision(&manifest).unwrap();
        provider.hydrate(&handle, &manifest).unwrap();

        fs::create_dir_all(handle.root.join("artifacts/output")).unwrap();
        fs::write(handle.root.join("artifacts/output/result.txt"), b"ok").unwrap();
        let artifacts = provider
            .collect(&handle, &[PathBuf::from("artifacts/output/result.txt")])
            .unwrap();
        assert_eq!(artifacts[0].bytes, b"ok");

        provider.destroy(&handle).unwrap();
        provider.destroy(&handle).unwrap();
    }

    #[test]
    fn executes_inside_declared_working_directory() {
        let root = tempfile::tempdir().unwrap();
        let provider =
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Test, true).unwrap();
        let manifest = manifest();
        let handle = provider.provision(&manifest).unwrap();
        let result = provider
            .execute(
                &handle,
                &CommandSpec {
                    program: "rustc".into(),
                    arguments: vec!["--version".into()],
                    environment: BTreeMap::new(),
                },
                Duration::from_secs(10),
            )
            .unwrap();
        assert_eq!(result.exit_code, Some(0));
        assert!(!result.timed_out);
        assert!(String::from_utf8_lossy(&result.stdout).starts_with("rustc "));
    }

    #[test]
    fn rejects_forged_handle_outside_provider_root() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let provider =
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Test, true).unwrap();
        let handle = WorkspaceHandle {
            id: "forged".into(),
            root: outside.path().join("forged"),
            working_directory: outside.path().join("forged/work"),
            provider_reference: None,
        };
        assert!(matches!(
            provider.destroy(&handle),
            Err(WorkspaceError::InvalidHandle(_))
        ));
    }

    #[cfg(unix)]
    #[test]
    fn passes_shared_functional_conformance() {
        use agentic_workspace_conformance::{assert_basic_lifecycle, minimal_manifest};

        let root = tempfile::tempdir().unwrap();
        let provider =
            LocalProvider::enable_insecure(root.path(), RuntimeMode::Test, true).unwrap();
        let manifest = minimal_manifest("local-conformance", SecurityProfile::InsecureLocal);
        assert_basic_lifecycle(
            &provider,
            &manifest,
            CommandSpec {
                program: "sh".into(),
                arguments: vec!["-c".into(), "printf 'conformant\\n' > artifact.txt".into()],
                environment: BTreeMap::new(),
            },
            "work/artifact.txt",
        )
        .unwrap();
    }
}
