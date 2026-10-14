"""Integration tests for plugin env var forwarding (ADR-033).

Validates that requires_env declared in plugin.json gets auto-forwarded
into Docker containers via resolve_plugin_env().

These tests require:
- Docker installed and running
- FIRECRAWL_API_KEY set in the environment

Run with: pytest tests/integration/test_plugin_env.py -v
"""

import os
from pathlib import Path

import pytest

from agentic_isolation import (
    AgenticWorkspace,
    SecurityConfig,
    WorkspaceConfig,
    WorkspaceDockerProvider,
)
from agentic_isolation.config import host_env_credential
from agentic_isolation.contract import CredentialBinding, CredentialRef
from agentic_isolation.providers import HostEnvironmentCredentials

EXPLICIT = CredentialBinding(
    ref=CredentialRef(name="FIRECRAWL_KEY_V2", scope="host-env"),
    delivery="env",
    target="FIRECRAWL_API_KEY",
)

# Path to the research plugin (has requires_env with FIRECRAWL_API_KEY)
RESEARCH_PLUGIN = Path(__file__).parent.parent.parent.parent.parent.parent / "plugins" / "research"


def docker_available() -> bool:
    """Check if Docker is available."""
    return WorkspaceDockerProvider.is_available()


def firecrawl_key_available() -> bool:
    """Check if FIRECRAWL_API_KEY is set."""
    return bool(os.environ.get("FIRECRAWL_API_KEY"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
    pytest.mark.skipif(not firecrawl_key_available(), reason="FIRECRAWL_API_KEY not set"),
]


def _forwarding_workspace() -> AgenticWorkspace:
    """A Docker workspace whose provider resolves host-env credential bindings."""
    config = WorkspaceConfig(
        provider="docker",
        image="python:3.12-slim",
        plugins=[str(RESEARCH_PLUGIN)],
        security=SecurityConfig.development(),
    )
    return AgenticWorkspace(
        config, provider=WorkspaceDockerProvider(credentials=HostEnvironmentCredentials())
    )


class TestPluginEnvForwarding:
    """Tests for requires_env auto-forwarding into Docker containers."""

    @pytest.mark.asyncio
    async def test_resolve_plugin_env_binds_credentials(self) -> None:
        """resolve_plugin_env() should bind matching host env vars by reference."""
        config = WorkspaceConfig(
            provider="docker",
            image="python:3.12-slim",
            plugins=[str(RESEARCH_PLUGIN)],
        )

        # Before: no credentials
        assert config.credentials == []

        # Resolve: reads plugin.json requires_env, matches host env
        config.resolve_plugin_env()

        # After: bound by name; the value stays in the host environment
        assert config.credentials == [host_env_credential("FIRECRAWL_API_KEY")]

    @pytest.mark.asyncio
    async def test_secret_available_inside_container(self) -> None:
        """FIRECRAWL_API_KEY should be accessible inside the Docker container."""
        async with _forwarding_workspace() as workspace:
            result = await workspace.execute("echo $FIRECRAWL_API_KEY")

            # Should be set and non-empty
            value = result.stdout.strip()
            assert value, "FIRECRAWL_API_KEY should be set inside container"
            assert value == os.environ["FIRECRAWL_API_KEY"]

    @pytest.mark.asyncio
    async def test_firecrawl_api_call_inside_container(self) -> None:
        """Should be able to call Firecrawl API from inside the container."""
        async with _forwarding_workspace() as workspace:
            # Write a test script to avoid shell quoting issues
            await workspace.write_file(
                "test_firecrawl.py",
                "import os\n"
                "from firecrawl import FirecrawlApp\n"
                "app = FirecrawlApp(api_key=os.environ['FIRECRAWL_API_KEY'])\n"
                "result = app.scrape('https://httpbin.org/html', formats=['markdown'])\n"
                "print(f'OK: {len(result.markdown)} chars')\n",
            )

            # Install firecrawl-py and run the test script
            result = await workspace.execute(
                "pip install -q firecrawl-py > /dev/null 2>&1 && python3 test_firecrawl.py",
                timeout=60,
            )

            assert result.exit_code == 0, f"Firecrawl call failed: {result.stderr}"
            assert "OK:" in result.stdout, f"Unexpected output: {result.stdout}"
            # Parse "OK: 1234 chars"
            chars = int(result.stdout.split("OK: ")[1].split(" chars")[0])
            assert chars > 100, f"Expected substantial content, got {chars} chars"


class TestPluginEnvEdgeCases:
    """Edge case tests for plugin env resolution."""

    @pytest.mark.asyncio
    async def test_missing_optional_env_not_fatal(self) -> None:
        """Optional env vars should not cause errors if not set."""
        config = WorkspaceConfig(
            provider="docker",
            image="python:3.12-slim",
            plugins=[str(RESEARCH_PLUGIN)],
        )

        # Temporarily unset the key to test optional behavior
        original = os.environ.pop("FIRECRAWL_API_KEY", None)
        try:
            # Should not raise (FIRECRAWL_API_KEY is required=false in plugin.json)
            config.resolve_plugin_env()
            assert config.credentials == []
        finally:
            if original:
                os.environ["FIRECRAWL_API_KEY"] = original

    @pytest.mark.asyncio
    async def test_explicit_secret_not_overwritten(self) -> None:
        """Explicitly bound credentials should not be overwritten by resolve_plugin_env()."""
        config = WorkspaceConfig(
            provider="docker",
            image="python:3.12-slim",
            plugins=[str(RESEARCH_PLUGIN)],
            credentials=[EXPLICIT],
        )

        config.resolve_plugin_env()

        # Should keep the explicit binding
        assert config.credentials == [EXPLICIT]

    @pytest.mark.asyncio
    async def test_plugin_without_requires_env(self) -> None:
        """Plugins without requires_env should work fine."""
        # sdlc plugin has no requires_env
        sdlc_plugin = RESEARCH_PLUGIN.parent / "sdlc"
        if not sdlc_plugin.exists():
            pytest.skip("sdlc plugin not found")

        config = WorkspaceConfig(
            provider="docker",
            image="python:3.12-slim",
            plugins=[str(sdlc_plugin)],
        )

        # Should not raise
        config.resolve_plugin_env()
        assert len(config.credentials) == 0
