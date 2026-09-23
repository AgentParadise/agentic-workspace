//! Docker implementation of the provider-neutral workspace lifecycle.

use agentic_workspace_core::{
    Artifact, CommandSpec, ExecutionResult, LaunchManifest, SecurityProfile, WorkspaceError,
    WorkspaceHandle, WorkspaceProvider, execute_process, materialize_file, validate_identifier,
    validate_relative_path,
};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

const CONTROL_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetworkPolicy {
    None,
    Bridge(String),
}

#[derive(Debug, Clone)]
pub struct DockerProvider {
    root: PathBuf,
    image: String,
    network: NetworkPolicy,
    docker: PathBuf,
}

impl DockerProvider {
    pub fn new(
        root: impl Into<PathBuf>,
        image: impl Into<String>,
        network: NetworkPolicy,
    ) -> Result<Self, WorkspaceError> {
        let root = root.into();
        fs::create_dir_all(&root).map_err(|source| Self::io(&root, source))?;
        let root = fs::canonicalize(&root).map_err(|source| Self::io(&root, source))?;
        Ok(Self {
            root,
            image: image.into(),
            network,
            docker: PathBuf::from("docker"),
        })
    }

    pub fn with_docker_binary(mut self, docker: impl Into<PathBuf>) -> Self {
        self.docker = docker.into();
        self
    }

    fn io(path: &Path, source: std::io::Error) -> WorkspaceError {
        WorkspaceError::Io {
            path: path.to_path_buf(),
            source,
        }
    }

    fn container_name(id: &str) -> String {
        format!("agentic-workspace-{id}")
    }

    fn validate_handle(&self, handle: &WorkspaceHandle) -> Result<String, WorkspaceError> {
        validate_identifier(&handle.id)?;
        let expected = Self::container_name(&handle.id);
        if handle.root.parent() != Some(self.root.as_path())
            || handle.root.file_name() != Some(handle.id.as_ref())
            || !handle.working_directory.starts_with(&handle.root)
            || handle.provider_reference.as_deref() != Some(expected.as_str())
        {
            return Err(WorkspaceError::InvalidHandle(handle.root.clone()));
        }
        Ok(expected)
    }

    fn docker_result(&self, arguments: &[String]) -> Result<ExecutionResult, WorkspaceError> {
        let mut command = Command::new(&self.docker);
        command.args(arguments);
        execute_process(&mut command, CONTROL_TIMEOUT, self.docker.clone())
    }

    fn require_success(
        &self,
        operation: &str,
        arguments: &[String],
    ) -> Result<ExecutionResult, WorkspaceError> {
        let result = self.docker_result(arguments)?;
        if result.exit_code == Some(0) && !result.timed_out {
            Ok(result)
        } else {
            Err(WorkspaceError::ProviderCommand {
                operation: operation.into(),
                stderr: String::from_utf8_lossy(&result.stderr).trim().into(),
            })
        }
    }

    fn ensure_supported(manifest: &LaunchManifest) -> Result<(), WorkspaceError> {
        if !manifest.tools.allow.is_empty()
            || !manifest.tools.deny.is_empty()
            || manifest.tools.sandbox.is_some()
        {
            return Err(WorkspaceError::Unsupported(
                "Docker provider cannot enforce this tool policy".into(),
            ));
        }
        if !manifest.capabilities.is_empty() {
            return Err(WorkspaceError::Unsupported(
                "Docker capability materialization is not implemented".into(),
            ));
        }
        if manifest.limits.disk_mb.is_some() {
            return Err(WorkspaceError::Unsupported(
                "portable Docker disk limits are not available".into(),
            ));
        }
        if !manifest.content.repositories.is_empty() || !manifest.skills.is_empty() {
            return Err(WorkspaceError::Unsupported(
                "repository and skill hydration are not implemented".into(),
            ));
        }
        Ok(())
    }
}

impl WorkspaceProvider for DockerProvider {
    fn name(&self) -> &'static str {
        "docker"
    }

    fn security_profile(&self) -> SecurityProfile {
        SecurityProfile::Isolated
    }

    fn provision(&self, manifest: &LaunchManifest) -> Result<WorkspaceHandle, WorkspaceError> {
        manifest.validate_boundary()?;
        Self::ensure_supported(manifest)?;
        if manifest.workspace.security_profile != SecurityProfile::Isolated {
            return Err(WorkspaceError::InvalidManifest(
                "Docker requires security_profile=isolated".into(),
            ));
        }

        let root = self.root.join(&manifest.execution_id);
        let working_directory = root.join(&manifest.workspace.working_directory);
        fs::create_dir_all(&working_directory)
            .map_err(|source| Self::io(&working_directory, source))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o777))
                .map_err(|source| Self::io(&root, source))?;
            fs::set_permissions(&working_directory, fs::Permissions::from_mode(0o777))
                .map_err(|source| Self::io(&working_directory, source))?;
        }

        let container = Self::container_name(&manifest.execution_id);
        let mut arguments = vec![
            "run".into(),
            "--detach".into(),
            format!("--name={container}"),
            "--cap-drop=ALL".into(),
            "--security-opt=no-new-privileges".into(),
            "--read-only".into(),
            "--pids-limit=256".into(),
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m".into(),
            format!("--volume={}:/workspace:rw", root.display()),
            format!(
                "--workdir=/workspace/{}",
                manifest.workspace.working_directory
            ),
            format!("--label=agentic.workspace.id={}", manifest.execution_id),
        ];
        match &self.network {
            NetworkPolicy::None => arguments.push("--network=none".into()),
            NetworkPolicy::Bridge(name) => arguments.push(format!("--network={name}")),
        }
        if let Some(memory_mb) = manifest.limits.memory_mb {
            arguments.push(format!("--memory={memory_mb}m"));
        }
        if let Some(cpu_millis) = manifest.limits.cpu_millis {
            arguments.push(format!("--cpus={}", f64::from(cpu_millis) / 1000.0));
        }
        arguments.push(self.image.clone());
        arguments.extend(["sleep".into(), "infinity".into()]);

        if let Err(error) = self.require_success("docker run", &arguments) {
            let _ = fs::remove_dir_all(&root);
            return Err(error);
        }
        Ok(WorkspaceHandle {
            id: manifest.execution_id.clone(),
            root,
            working_directory,
            provider_reference: Some(container),
        })
    }

    fn hydrate(
        &self,
        handle: &WorkspaceHandle,
        manifest: &LaunchManifest,
    ) -> Result<(), WorkspaceError> {
        self.validate_handle(handle)?;
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
        let container = self.validate_handle(handle)?;
        let relative = handle
            .working_directory
            .strip_prefix(&handle.root)
            .map_err(|_| WorkspaceError::InvalidHandle(handle.root.clone()))?;
        let mut process = Command::new(&self.docker);
        process.args([
            "exec",
            "--workdir",
            &format!("/workspace/{}", relative.display()),
        ]);
        for (name, value) in &command.environment {
            process.args(["--env", &format!("{name}={value}")]);
        }
        process
            .arg(&container)
            .arg(&command.program)
            .args(&command.arguments);
        let result = execute_process(&mut process, timeout, self.docker.clone())?;
        if result.timed_out {
            let _ = self.docker_result(&["kill".into(), container]);
        }
        Ok(result)
    }

    fn collect(
        &self,
        handle: &WorkspaceHandle,
        relative_paths: &[PathBuf],
    ) -> Result<Vec<Artifact>, WorkspaceError> {
        self.validate_handle(handle)?;
        relative_paths
            .iter()
            .map(|relative_path| {
                validate_relative_path(&relative_path.to_string_lossy())?;
                let path = handle.root.join(relative_path);
                let canonical =
                    fs::canonicalize(&path).map_err(|source| Self::io(&path, source))?;
                if !canonical.starts_with(&handle.root) {
                    return Err(WorkspaceError::InvalidHandle(canonical));
                }
                let bytes = fs::read(&canonical).map_err(|source| Self::io(&canonical, source))?;
                Ok(Artifact {
                    relative_path: relative_path.clone(),
                    bytes,
                })
            })
            .collect()
    }

    fn destroy(&self, handle: &WorkspaceHandle) -> Result<(), WorkspaceError> {
        let container = self.validate_handle(handle)?;
        let result = self.docker_result(&["rm".into(), "--force".into(), container])?;
        let stderr = String::from_utf8_lossy(&result.stderr);
        if result.exit_code != Some(0) && !stderr.contains("No such container") {
            return Err(WorkspaceError::ProviderCommand {
                operation: "docker rm".into(),
                stderr: stderr.trim().into(),
            });
        }
        if handle.root.exists() {
            fs::remove_dir_all(&handle.root).map_err(|source| Self::io(&handle.root, source))?;
        }
        Ok(())
    }
}
