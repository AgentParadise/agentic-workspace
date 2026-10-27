"""Workspace providers for isolated execution.

Available providers:
- WorkspaceLocalProvider: Local filesystem (development/testing)
- WorkspaceDockerProvider: Docker containers (production)
- InteractiveTmuxProvider: Docker containers running interactive claude/codex/
  gemini CLIs in tmux panes (productionized from EXP-01..EXP-04; adapter
  added per the EXP-05 codex cross-review M2 finding)
- E2BWorkspaceProvider: E2B cloud sandboxes (syntropic137/syntropic137#1915)
"""

from typing import TYPE_CHECKING, Any

from agentic_isolation.harnesses import (
    AgentName,
    HarnessPlugin,
    HarnessTranscript,
    TranscriptExtractionResult,
    TranscriptSource,
    exec_argv,
    get_harness,
    register_harness,
)
from agentic_isolation.providers.base import (
    AwaitResult,
    CredentialPolicyError,
    CredentialResolver,
    ExecuteResult,
    HostEnvironmentCredentials,
    InteractiveSession,
    SupportsStagedTeardown,
    SupportsWorkspaceLogs,
    UnsupportedLimitError,
    Workspace,
    WorkspaceProvider,
)
from agentic_isolation.providers.capabilities import (
    OwnedWorkspace,
    StreamOutcome,
    StreamReconnectUnsupportedError,
    StreamSession,
    SupportsFileTransfer,
    SupportsOperationReattach,
    SupportsOwnership,
    SupportsStreamingExec,
    SupportsWorkspaceSuspend,
    WorkspaceNotFoundError,
    WorkspaceOwnershipError,
)
from agentic_isolation.providers.docker import ContainerRemovalError, WorkspaceDockerProvider
from agentic_isolation.providers.e2b import (
    E2BStreamSession,
    E2BWorkspaceProvider,
    SandboxRemovalError,
)
from agentic_isolation.providers.e2b_client import (
    CommandExit,
    E2BSandboxClient,
    E2BTransportError,
    SandboxCommand,
    SandboxListing,
    SdkE2BClient,
)
from agentic_isolation.providers.e2b_template import (
    DELTA_PATH,
    DOCKER_APT_KEY_FINGERPRINT,
    DOCKER_READY_CMD,
    NESTED_DOCKER,
    VERSIONS_PATH,
    DockerEngine,
    E2BTemplateBuild,
    E2BTemplateSpec,
    PrePull,
    RegistryCredentialsEnv,
    TemplateSpecError,
    build_template,
    manifest_hash,
)
from agentic_isolation.providers.file_transfer import (
    FileRefusal,
    ReadFilesError,
    ReadFilesResult,
    ReadFilesTimeoutError,
    ReadLimits,
)
from agentic_isolation.providers.local import WorkspaceLocalProvider
from agentic_isolation.providers.teardown_report import (
    DeleteFallback,
    TeardownReport,
    TeardownUsage,
)

if TYPE_CHECKING:
    from agentic_isolation.providers.interactive_tmux import InteractiveTmuxProvider


def __getattr__(name: str) -> Any:
    """Lazy export for providers with optional external dependencies.

    `InteractiveTmuxProvider` depends on the repo-root single-file driver
    (implementations/docker/interactive-tmux/driver/interactive_tmux.py),
    which is NOT shipped inside the agentic-isolation wheel. Importing it
    eagerly would break `import agentic_isolation` for every installed
    consumer, so it is resolved on first attribute access instead.
    """
    if name == "InteractiveTmuxProvider":
        from agentic_isolation.providers.interactive_tmux import (
            InteractiveTmuxProvider,
        )

        return InteractiveTmuxProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Base types
    "WorkspaceProvider",
    "SupportsStagedTeardown",
    "DeleteFallback",
    "TeardownReport",
    "TeardownUsage",
    "SupportsWorkspaceLogs",
    # Remote-tier capabilities (syntropic137/syntropic137#1911)
    "SupportsStreamingExec",
    "SupportsWorkspaceSuspend",
    "StreamSession",
    "StreamOutcome",
    "StreamReconnectUnsupportedError",
    "SupportsFileTransfer",
    "ReadLimits",
    "ReadFilesResult",
    "FileRefusal",
    "ReadFilesError",
    "ReadFilesTimeoutError",
    "SupportsOperationReattach",
    "SupportsOwnership",
    "OwnedWorkspace",
    "WorkspaceNotFoundError",
    "WorkspaceOwnershipError",
    "Workspace",
    "ExecuteResult",
    "InteractiveSession",
    "AwaitResult",
    "CredentialResolver",
    "HostEnvironmentCredentials",
    "CredentialPolicyError",
    "UnsupportedLimitError",
    # Providers
    "WorkspaceLocalProvider",
    "WorkspaceDockerProvider",
    "ContainerRemovalError",
    "InteractiveTmuxProvider",
    # E2B (syntropic137/syntropic137#1915)
    "E2BWorkspaceProvider",
    "E2BStreamSession",
    "SandboxRemovalError",
    "E2BSandboxClient",
    "SandboxCommand",
    "SandboxListing",
    "CommandExit",
    "E2BTransportError",
    "SdkE2BClient",
    "E2BTemplateSpec",
    "E2BTemplateBuild",
    "DockerEngine",
    "PrePull",
    "RegistryCredentialsEnv",
    "NESTED_DOCKER",
    "DOCKER_APT_KEY_FINGERPRINT",
    "DOCKER_READY_CMD",
    "DELTA_PATH",
    "VERSIONS_PATH",
    "TemplateSpecError",
    "build_template",
    "manifest_hash",
    # Harness contract (see agentic_isolation.harnesses, issue #792)
    "AgentName",
    "HarnessPlugin",
    "HarnessTranscript",
    "TranscriptExtractionResult",
    "TranscriptSource",
    "exec_argv",
    "get_harness",
    "register_harness",
]
