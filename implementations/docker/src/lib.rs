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

/// Docker's default seccomp profile (docker-v29.8.0) plus one rule allowing
/// `clone` (namespace flags), `unshare`, `mount`, `umount2` and `pivot_root`,
/// so a harness that sandboxes itself with bubblewrap (Codex) can create a
/// user namespace. Capabilities stay dropped. Single source shared with the
/// Python package; provenance in its `seccomp/README.md`.
pub const CODEX_SANDBOX_SECCOMP_PROFILE: &str = include_str!(
    "../../../lib/python/agentic_isolation/agentic_isolation/seccomp/codex-sandbox.json"
);
pub const CODEX_SANDBOX_SECCOMP_FILE_NAME: &str = "codex-sandbox.json";

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
    seccomp_profile: Option<PathBuf>,
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
            seccomp_profile: None,
        })
    }

    pub fn with_docker_binary(mut self, docker: impl Into<PathBuf>) -> Self {
        self.docker = docker.into();
        self
    }

    /// Run containers with this seccomp profile instead of Docker's default.
    /// The file is read by the docker CLI on this host, so it must exist here.
    pub fn with_seccomp_profile(
        mut self,
        profile: impl Into<PathBuf>,
    ) -> Result<Self, WorkspaceError> {
        let profile = profile.into();
        let canonical = fs::canonicalize(&profile).map_err(|source| Self::io(&profile, source))?;
        if !canonical.is_file() {
            return Err(WorkspaceError::UnsafePath(canonical.display().to_string()));
        }
        self.seccomp_profile = Some(canonical);
        Ok(self)
    }

    /// Opt in to [`CODEX_SANDBOX_SECCOMP_PROFILE`] for workspaces that can run
    /// Codex. Writes the profile into `directory`, which the caller owns and
    /// which must not be writable by workspace code. Only for Codex-capable
    /// workspaces; everything else keeps Docker's default profile.
    pub fn with_codex_sandbox_seccomp(
        self,
        directory: impl AsRef<Path>,
    ) -> Result<Self, WorkspaceError> {
        let directory = directory.as_ref();
        fs::create_dir_all(directory).map_err(|source| Self::io(directory, source))?;
        let path = directory.join(CODEX_SANDBOX_SECCOMP_FILE_NAME);
        fs::write(&path, CODEX_SANDBOX_SECCOMP_PROFILE)
            .map_err(|source| Self::io(&path, source))?;
        self.with_seccomp_profile(path)
    }

    pub fn seccomp_profile(&self) -> Option<&Path> {
        self.seccomp_profile.as_deref()
    }

    fn security_arguments(&self) -> Vec<String> {
        let mut arguments = vec![
            "--cap-drop=ALL".into(),
            "--security-opt=no-new-privileges".into(),
        ];
        if let Some(profile) = &self.seccomp_profile {
            arguments.push(format!("--security-opt=seccomp={}", profile.display()));
        }
        arguments.extend([
            "--read-only".into(),
            "--pids-limit=256".into(),
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m".into(),
        ]);
        arguments
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
        ];
        arguments.extend(self.security_arguments());
        arguments.extend([
            format!("--volume={}:/workspace:rw", root.display()),
            format!(
                "--workdir=/workspace/{}",
                manifest.workspace.working_directory
            ),
            format!("--label=agentic.workspace.id={}", manifest.execution_id),
        ]);
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

#[cfg(test)]
mod tests {
    use super::*;

    fn provider(root: &Path) -> DockerProvider {
        DockerProvider::new(root.join("root"), "image", NetworkPolicy::None).unwrap()
    }

    #[test]
    fn default_keeps_docker_default_seccomp() {
        let dir = tempfile::tempdir().unwrap();
        let arguments = provider(dir.path()).security_arguments();
        assert!(
            !arguments
                .iter()
                .any(|a| a.starts_with("--security-opt=seccomp"))
        );
        assert_eq!(
            arguments,
            [
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--read-only",
                "--pids-limit=256",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            ]
        );
    }

    #[test]
    fn codex_sandbox_adds_only_the_profile() {
        let dir = tempfile::tempdir().unwrap();
        let plain = provider(dir.path()).security_arguments();
        let codex = provider(dir.path())
            .with_codex_sandbox_seccomp(dir.path().join("seccomp"))
            .unwrap();
        let profile = codex.seccomp_profile().unwrap().to_path_buf();
        assert_eq!(
            fs::read_to_string(&profile).unwrap(),
            CODEX_SANDBOX_SECCOMP_PROFILE
        );
        let arguments = codex.security_arguments();
        let seccomp: Vec<_> = arguments
            .iter()
            .filter(|a| a.starts_with("--security-opt=seccomp"))
            .collect();
        assert_eq!(
            seccomp,
            [&format!("--security-opt=seccomp={}", profile.display())]
        );
        let rest: Vec<_> = arguments
            .iter()
            .filter(|a| !a.starts_with("--security-opt=seccomp"))
            .cloned()
            .collect();
        assert_eq!(rest, plain);
    }

    #[test]
    fn missing_profile_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        assert!(
            provider(dir.path())
                .with_seccomp_profile(dir.path().join("absent.json"))
                .is_err()
        );
        assert!(
            provider(dir.path())
                .with_seccomp_profile(dir.path())
                .is_err()
        );
    }

    #[test]
    fn embedded_profile_adds_exactly_the_five_syscalls() {
        let marker = "\"comment\": \"agentic-isolation:";
        assert_eq!(CODEX_SANDBOX_SECCOMP_PROFILE.matches(marker).count(), 1);
        let rule_start = CODEX_SANDBOX_SECCOMP_PROFILE
            .rfind("\"names\"")
            .expect("rule");
        let rule = &CODEX_SANDBOX_SECCOMP_PROFILE[rule_start..];
        for name in ["clone", "mount", "pivot_root", "umount2", "unshare"] {
            assert!(rule.contains(&format!("\"{name}\"")), "{name} missing");
        }
        for name in ["setns", "clone3", "bpf"] {
            assert!(!rule.contains(&format!("\"{name}\"")), "{name} added");
        }
        assert!(rule.contains("SCMP_ACT_ALLOW"));
        assert!(!rule.contains("\"args\""));
        assert!(CODEX_SANDBOX_SECCOMP_PROFILE.contains("\"defaultAction\": \"SCMP_ACT_ERRNO\""));
    }
}
