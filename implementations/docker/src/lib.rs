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

/// Docker's default seccomp profile (docker-v29.8.0) plus `clone`/`unshare`
/// for the user, mount, pid, net and ipc namespaces only, and `mount`,
/// `umount2`, `pivot_root`, so a harness that sandboxes itself with
/// bubblewrap (Codex) can build its namespaces. Capabilities stay dropped. Single source shared with the
/// Python package; provenance in its `seccomp/README.md`.
pub const CODEX_SANDBOX_SECCOMP_PROFILE: &str = include_str!(
    "../../../lib/python/agentic_isolation/agentic_isolation/seccomp/codex-sandbox.json"
);
pub const CODEX_SANDBOX_SECCOMP_FILE_NAME: &str = "codex-sandbox.json";

/// Docker's docker-default AppArmor profile with `deny mount,` replaced by
/// the mounts bubblewrap performs. Paired with the seccomp profile on hosts
/// where Docker uses AppArmor. Single source shared with the Python package;
/// provenance in its `apparmor/README.md`.
pub const CODEX_SANDBOX_APPARMOR_PROFILE: &str = include_str!(
    "../../../lib/python/agentic_isolation/agentic_isolation/apparmor/agentic-codex-sandbox"
);
pub const CODEX_SANDBOX_APPARMOR_PROFILE_NAME: &str = "agentic-codex-sandbox";
const APPARMOR_POLICY_PROFILES: &str = "/sys/kernel/security/apparmor/policy/profiles";

/// Whether `profile` is loaded in this kernel. `None` when this host cannot
/// tell (securityfs absent or unreadable, for example a remote daemon).
pub fn apparmor_profile_loaded(policy_profiles: &Path, profile: &str) -> Option<bool> {
    let entries = fs::read_dir(policy_profiles).ok()?;
    Some(entries.flatten().any(|entry| {
        fs::read_to_string(entry.path().join("name")).is_ok_and(|name| name.trim() == profile)
    }))
}

fn apparmor_not_loaded(profile: &str, file: &Path) -> WorkspaceError {
    WorkspaceError::Unsupported(format!(
        "AppArmor is active on the Docker host but profile {profile} is not loaded; \
         load it on the Docker host with `sudo apparmor_parser -r {}` and retry",
        file.display()
    ))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetworkPolicy {
    None,
    Bridge(String),
}

/// Image label that declares a workspace image can run Codex. The provider
/// reads it at provision and derives the Codex sandbox policy from it.
pub const CODEX_IMAGE_LABEL: &str = "agentic.codex_cli_version";
/// Where profiles are written when no directory is configured: beside the
/// execution roots, never inside one (an execution with this id is refused).
const DEFAULT_PROFILE_DIR: &str = ".agentic-codex-sandbox";

/// The Codex sandbox policy materialized for one provision.
#[derive(Debug, Clone, PartialEq, Eq)]
struct CodexPolicy {
    seccomp: PathBuf,
    apparmor: Option<String>,
    apparmor_file: PathBuf,
}

#[derive(Debug, Clone)]
pub struct DockerProvider {
    root: PathBuf,
    image: String,
    network: NetworkPolicy,
    docker: PathBuf,
    /// None derives the policy from the image label; Some must agree with it.
    codex_sandbox: Option<bool>,
    codex_profile_dir: Option<PathBuf>,
    apparmor_policy_profiles: PathBuf,
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
            codex_sandbox: None,
            codex_profile_dir: None,
            apparmor_policy_profiles: PathBuf::from(APPARMOR_POLICY_PROFILES),
        })
    }

    pub fn with_docker_binary(mut self, docker: impl Into<PathBuf>) -> Self {
        self.docker = docker.into();
        self
    }

    /// Assert the image can run Codex and write the Codex sandbox profiles into
    /// `directory` (owned by the caller, not writable by workspace code). The
    /// policy itself always follows the image's [`CODEX_IMAGE_LABEL`]:
    /// provisioning an image without it is refused.
    pub fn with_codex_sandbox(mut self, directory: impl Into<PathBuf>) -> Self {
        self.codex_sandbox = Some(true);
        self.codex_profile_dir = Some(directory.into());
        self
    }

    /// Assert the image cannot run Codex; provisioning one that declares
    /// [`CODEX_IMAGE_LABEL`] is refused.
    pub fn without_codex_sandbox(mut self) -> Self {
        self.codex_sandbox = Some(false);
        self
    }

    /// Where to write the profiles when the policy is derived from the image.
    pub fn with_codex_profile_dir(mut self, directory: impl Into<PathBuf>) -> Self {
        self.codex_profile_dir = Some(directory.into());
        self
    }

    fn resolve_codex(requested: Option<bool>, capable: bool) -> Result<bool, WorkspaceError> {
        match (requested, capable) {
            (Some(true), false) => Err(WorkspaceError::InvalidManifest(format!(
                "the Codex sandbox was requested but the image has no {CODEX_IMAGE_LABEL} label"
            ))),
            (Some(false), true) => Err(WorkspaceError::InvalidManifest(format!(
                "the image declares {CODEX_IMAGE_LABEL}, so it must run with the Codex sandbox policy"
            ))),
            _ => Ok(capable),
        }
    }

    /// The immutable image ID and whether that image declares Codex; pulls
    /// once if absent. Provisioning launches by this ID, so the label that
    /// decided the policy belongs to exactly the image that runs, even if the
    /// tag moves in between. Fails closed on anything unreadable.
    fn inspect_image(&self) -> Result<(String, bool), WorkspaceError> {
        let inspect: Vec<String> = vec![
            "image".into(),
            "inspect".into(),
            "--format".into(),
            // `json .Config`, not `.Config.Labels`: some Docker versions omit
            // the Labels key entirely for unlabelled images.
            "{{.Id}} {{json .Config}}".into(),
            self.image.clone(),
        ];
        let first = self.docker_result(&inspect)?;
        let result = if first.exit_code == Some(0) && !first.timed_out {
            first
        } else {
            let _ = self.docker_result(&["pull".into(), "--quiet".into(), self.image.clone()]);
            self.require_success("docker image inspect", &inspect)?
        };
        let output = String::from_utf8_lossy(&result.stdout).trim().to_owned();
        let unreadable = || WorkspaceError::ProviderCommand {
            operation: "docker image inspect".into(),
            stderr: format!("unreadable inspect output for {}", self.image),
        };
        let (id, config) = output.split_once(' ').ok_or_else(unreadable)?;
        if !id.starts_with("sha256:") || id.len() != 71 {
            return Err(unreadable());
        }
        let config: serde_json::Value = serde_json::from_str(config).map_err(|_| unreadable())?;
        let capable = config
            .get("Labels")
            .and_then(|labels| labels.get(CODEX_IMAGE_LABEL))
            .and_then(serde_json::Value::as_str)
            .is_some_and(|value| !value.is_empty());
        Ok((id.to_owned(), capable))
    }

    /// Write the profiles and settle AppArmor for one provision. Nothing is
    /// cached: a failed daemon query fails this provision, never downgrades.
    fn codex_policy(&self) -> Result<CodexPolicy, WorkspaceError> {
        let directory = self
            .codex_profile_dir
            .clone()
            .unwrap_or_else(|| self.root.join(DEFAULT_PROFILE_DIR));
        fs::create_dir_all(&directory).map_err(|source| Self::io(&directory, source))?;
        let seccomp = directory.join(CODEX_SANDBOX_SECCOMP_FILE_NAME);
        fs::write(&seccomp, CODEX_SANDBOX_SECCOMP_PROFILE)
            .map_err(|source| Self::io(&seccomp, source))?;
        let apparmor_file = directory.join(CODEX_SANDBOX_APPARMOR_PROFILE_NAME);
        fs::write(&apparmor_file, CODEX_SANDBOX_APPARMOR_PROFILE)
            .map_err(|source| Self::io(&apparmor_file, source))?;
        let seccomp = fs::canonicalize(&seccomp).map_err(|source| Self::io(&seccomp, source))?;
        let apparmor = if self.docker_apparmor_active()? {
            if apparmor_profile_loaded(
                &self.apparmor_policy_profiles,
                CODEX_SANDBOX_APPARMOR_PROFILE_NAME,
            ) == Some(false)
            {
                return Err(apparmor_not_loaded(
                    CODEX_SANDBOX_APPARMOR_PROFILE_NAME,
                    &apparmor_file,
                ));
            }
            Some(CODEX_SANDBOX_APPARMOR_PROFILE_NAME.to_owned())
        } else {
            None
        };
        Ok(CodexPolicy {
            seccomp,
            apparmor,
            apparmor_file,
        })
    }

    /// Whether the Docker daemon confines containers with AppArmor. Asks the
    /// daemon, so it is right for remote hosts; Docker Desktop reports none.
    fn docker_apparmor_active(&self) -> Result<bool, WorkspaceError> {
        let result = self.require_success(
            "docker info",
            &[
                "info".into(),
                "--format".into(),
                "{{json .SecurityOptions}}".into(),
            ],
        )?;
        Ok(String::from_utf8_lossy(&result.stdout).contains("name=apparmor"))
    }

    fn security_arguments(codex: Option<&CodexPolicy>) -> Vec<String> {
        let mut arguments = vec![
            "--cap-drop=ALL".into(),
            "--security-opt=no-new-privileges".into(),
        ];
        if let Some(policy) = codex {
            arguments.push(format!(
                "--security-opt=seccomp={}",
                policy.seccomp.display()
            ));
            if let Some(profile) = &policy.apparmor {
                arguments.push(format!("--security-opt=apparmor={profile}"));
            }
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
        if manifest.execution_id == DEFAULT_PROFILE_DIR {
            return Err(WorkspaceError::InvalidManifest(format!(
                "execution id {DEFAULT_PROFILE_DIR} is reserved"
            )));
        }
        // The Codex sandbox policy follows the image's own declaration and is
        // settled before anything is created, so a mismatch leaves nothing.
        let (image_id, codex_capable) = self.inspect_image()?;
        let codex = if Self::resolve_codex(self.codex_sandbox, codex_capable)? {
            Some(self.codex_policy()?)
        } else {
            None
        };

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
        arguments.extend(Self::security_arguments(codex.as_ref()));
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
        // The inspected image, not whatever the tag points at now.
        arguments.push(image_id);
        arguments.extend(["sleep".into(), "infinity".into()]);

        if let Err(error) = self.require_success("docker run", &arguments) {
            let _ = fs::remove_dir_all(&root);
            if let (WorkspaceError::ProviderCommand { stderr, .. }, Some(policy)) = (&error, &codex)
            {
                if let Some(profile) = &policy.apparmor {
                    if stderr.contains("apparmor failed to apply profile") {
                        return Err(apparmor_not_loaded(profile, &policy.apparmor_file));
                    }
                }
            }
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
    use agentic_workspace_conformance::minimal_manifest;

    #[test]
    fn default_keeps_docker_default_seccomp() {
        assert_eq!(
            DockerProvider::security_arguments(None),
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
    fn codex_policy_adds_only_its_profiles() {
        let policy = CodexPolicy {
            seccomp: PathBuf::from("/p/codex-sandbox.json"),
            apparmor: Some(CODEX_SANDBOX_APPARMOR_PROFILE_NAME.into()),
            apparmor_file: PathBuf::from("/p/agentic-codex-sandbox"),
        };
        let arguments = DockerProvider::security_arguments(Some(&policy));
        let (added, rest): (Vec<_>, Vec<_>) = arguments.into_iter().partition(|a| {
            a.starts_with("--security-opt=seccomp") || a.starts_with("--security-opt=apparmor")
        });
        assert_eq!(
            added,
            [
                "--security-opt=seccomp=/p/codex-sandbox.json",
                "--security-opt=apparmor=agentic-codex-sandbox",
            ]
        );
        assert_eq!(rest, DockerProvider::security_arguments(None));
    }

    #[test]
    fn policy_resolution_matrix() {
        assert!(DockerProvider::resolve_codex(None, true).unwrap());
        assert!(!DockerProvider::resolve_codex(None, false).unwrap());
        assert!(DockerProvider::resolve_codex(Some(true), true).unwrap());
        assert!(!DockerProvider::resolve_codex(Some(false), false).unwrap());
        assert!(DockerProvider::resolve_codex(Some(true), false).is_err());
        assert!(DockerProvider::resolve_codex(Some(false), true).is_err());
    }

    #[test]
    fn embedded_profiles_match_their_contract() {
        assert!(CODEX_SANDBOX_SECCOMP_PROFILE.contains("SCMP_CMP_MASKED_EQ"));
        let rules: Vec<&str> = CODEX_SANDBOX_APPARMOR_PROFILE
            .lines()
            .map(|line| line.split('#').next().unwrap_or("").trim())
            .filter(|line| !line.is_empty())
            .collect();
        assert!(!rules.contains(&"deny mount,"));
        assert!(!rules.contains(&"mount,"));
        assert!(!rules.contains(&"userns,"));
        assert!(rules.contains(&"deny mount /**/docker.sock -> /**,"));
        assert!(rules.contains(&"deny /sys/kernel/security/** rwklx,"));
        assert!(!rules.iter().any(|r| r.starts_with("mount")
            && r.contains("rbind")
            && r.ends_with("-> /newroot/{,**},")));
    }

    /// A fake docker CLI: records `run` arguments, answers `image inspect`,
    /// `info` from baked-in answers. `info` fails while `info-fail` exists.
    #[cfg(unix)]
    fn fake_docker(dir: &Path, label: &str, info: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let script = dir.join("docker");
        fs::write(
            &script,
            format!(
                "#!/bin/sh\n\
                 case \"$1 $2\" in\n\
                 \"image inspect\") n=$(cat '{d}/inspects' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '{d}/inspects'; printf 'sha256:%064d %s\\n' $n '{label}';;\n\
                 \"pull --quiet\") exit 1;;\n\
                 \"info --format\") [ -e '{d}/info-fail' ] && {{ echo daemon down >&2; exit 1; }}; printf '%s\\n' '{info}';;\n\
                 run*) printf '%s\\n' \"$@\" > '{d}/run-args'; echo id;;\n\
                 *) exit 0;;\n\
                 esac\n",
                d = dir.display()
            ),
        )
        .unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        script
    }

    #[cfg(unix)]
    fn provider(dir: &Path, label: &str, info: &str) -> DockerProvider {
        // `docker image inspect` prints `.Config` as JSON; an unlabelled
        // image may omit Labels entirely.
        let config = if label == "<no value>" {
            "{}".to_owned()
        } else {
            format!("{{\"Labels\":{{\"{CODEX_IMAGE_LABEL}\":\"{label}\"}}}}")
        };
        let docker = fake_docker(dir, &config, info);
        DockerProvider::new(dir.join("root"), "image", NetworkPolicy::None)
            .unwrap()
            .with_docker_binary(docker)
    }

    #[cfg(unix)]
    fn run_args(dir: &Path) -> Vec<String> {
        fs::read_to_string(dir.join("run-args"))
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    #[cfg(unix)]
    fn loaded_policy(dir: &Path, names: &[&str]) -> PathBuf {
        let policy = dir.join("policy");
        fs::create_dir_all(&policy).unwrap();
        for (index, name) in names.iter().enumerate() {
            let entry = policy.join(format!("{name}.{index}"));
            fs::create_dir_all(&entry).unwrap();
            fs::write(entry.join("name"), format!("{name}\n")).unwrap();
        }
        policy
    }

    #[cfg(unix)]
    #[test]
    fn codex_image_derives_policy_through_provision() {
        let dir = tempfile::tempdir().unwrap();
        let provider = provider(dir.path(), "0.156.1", "[\"name=seccomp\"]");
        provider
            .provision(&minimal_manifest("codex", SecurityProfile::Isolated))
            .unwrap();
        let args = run_args(dir.path());
        let seccomp: Vec<_> = args
            .iter()
            .filter(|a| a.starts_with("--security-opt=seccomp="))
            .collect();
        assert_eq!(seccomp.len(), 1);
        let file = seccomp[0].trim_start_matches("--security-opt=seccomp=");
        assert_eq!(
            fs::read_to_string(file).unwrap(),
            CODEX_SANDBOX_SECCOMP_PROFILE
        );
        assert!(!args.iter().any(|a| a.contains("apparmor")));
        assert!(args.contains(&"--cap-drop=ALL".to_owned()));
    }

    #[cfg(unix)]
    #[test]
    fn launches_the_inspected_image_id_not_the_tag() {
        let dir = tempfile::tempdir().unwrap();
        let provider = provider(dir.path(), "0.156.1", "[]");
        provider
            .provision(&minimal_manifest("by-id", SecurityProfile::Isolated))
            .unwrap();
        // The fake moves the tag on every inspect; only one inspect happened,
        // and the run used its ID.
        assert_eq!(
            fs::read_to_string(dir.path().join("inspects"))
                .unwrap()
                .trim(),
            "1"
        );
        let args = run_args(dir.path());
        assert!(!args.contains(&"image".to_owned()), "{args:?}");
        assert!(args.contains(&format!("sha256:{:064}", 1)), "{args:?}");
    }

    #[cfg(unix)]
    #[test]
    fn plain_image_keeps_docker_defaults_through_provision() {
        let dir = tempfile::tempdir().unwrap();
        let provider = provider(dir.path(), "<no value>", "[]");
        provider
            .provision(&minimal_manifest("plain", SecurityProfile::Isolated))
            .unwrap();
        assert!(
            !run_args(dir.path())
                .iter()
                .any(|a| a.contains("seccomp") || a.contains("apparmor"))
        );
    }

    #[cfg(unix)]
    #[test]
    fn mismatches_are_rejected_before_launch() {
        for (label, explicit) in [("<no value>", true), ("0.156.1", false)] {
            let dir = tempfile::tempdir().unwrap();
            let provider = provider(dir.path(), label, "[]");
            let provider = if explicit {
                provider.with_codex_sandbox(dir.path().join("profiles"))
            } else {
                provider.without_codex_sandbox()
            };
            let error = provider
                .provision(&minimal_manifest("mismatch", SecurityProfile::Isolated))
                .unwrap_err();
            assert!(error.to_string().contains(CODEX_IMAGE_LABEL), "{error}");
            assert!(!dir.path().join("run-args").exists());
            assert!(!dir.path().join("root/mismatch").exists());
        }
    }

    #[cfg(unix)]
    #[test]
    fn apparmor_host_applies_the_loaded_profile() {
        let dir = tempfile::tempdir().unwrap();
        let mut provider = provider(dir.path(), "0.156.1", "[\"name=apparmor\"]");
        provider.apparmor_policy_profiles = loaded_policy(
            dir.path(),
            &["docker-default", CODEX_SANDBOX_APPARMOR_PROFILE_NAME],
        );
        provider
            .provision(&minimal_manifest("aa", SecurityProfile::Isolated))
            .unwrap();
        assert!(
            run_args(dir.path())
                .contains(&"--security-opt=apparmor=agentic-codex-sandbox".to_owned())
        );
    }

    #[cfg(unix)]
    #[test]
    fn apparmor_host_without_the_profile_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let mut provider = provider(dir.path(), "0.156.1", "[\"name=apparmor\"]");
        provider.apparmor_policy_profiles = loaded_policy(dir.path(), &["docker-default"]);
        let error = provider
            .provision(&minimal_manifest("aa", SecurityProfile::Isolated))
            .unwrap_err();
        assert!(error.to_string().contains("apparmor_parser -r"), "{error}");
        assert!(!dir.path().join("run-args").exists());
    }

    #[cfg(unix)]
    #[test]
    fn failed_apparmor_detection_fails_then_recovers() {
        let dir = tempfile::tempdir().unwrap();
        let mut provider = provider(dir.path(), "0.156.1", "[\"name=apparmor\"]");
        provider.apparmor_policy_profiles =
            loaded_policy(dir.path(), &[CODEX_SANDBOX_APPARMOR_PROFILE_NAME]);
        fs::write(dir.path().join("info-fail"), "").unwrap();
        let error = provider
            .provision(&minimal_manifest("first", SecurityProfile::Isolated))
            .unwrap_err();
        assert!(error.to_string().contains("daemon down"), "{error}");
        assert!(!dir.path().join("run-args").exists());
        fs::remove_file(dir.path().join("info-fail")).unwrap();
        provider
            .provision(&minimal_manifest("second", SecurityProfile::Isolated))
            .unwrap();
        assert!(
            run_args(dir.path())
                .contains(&"--security-opt=apparmor=agentic-codex-sandbox".to_owned())
        );
    }

    #[cfg(unix)]
    #[test]
    fn unreadable_image_labels_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let docker = dir.path().join("docker");
        fs::write(&docker, "#!/bin/sh\nexit 1\n").unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
        }
        let provider = DockerProvider::new(dir.path().join("root"), "image", NetworkPolicy::None)
            .unwrap()
            .with_docker_binary(docker);
        assert!(
            provider
                .provision(&minimal_manifest("x", SecurityProfile::Isolated))
                .is_err()
        );
    }

    #[test]
    fn reserved_profile_directory_id_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let provider =
            DockerProvider::new(dir.path().join("root"), "image", NetworkPolicy::None).unwrap();
        let error = provider
            .provision(&minimal_manifest(
                DEFAULT_PROFILE_DIR,
                SecurityProfile::Isolated,
            ))
            .unwrap_err();
        assert!(error.to_string().contains("reserved"), "{error}");
    }
}
