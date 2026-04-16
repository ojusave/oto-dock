"""Dashboard adapter for the web chat interface.

Uses hook-based permission gating with --dangerously-skip-permissions.
The PreToolUse hook calls the proxy, which blocks (long-polls) until the dashboard user
clicks Allow/Deny.  Serves files directly from the proxy via token URLs.

Session configuration:
  - use_native_permissions = False (hook-based gating, not native CLI)
  - default_permission_mode = "default" (user controls via mode switcher)
  - hook_mode = "default" (hook blocks for dashboard permission prompts)
"""

from pathlib import Path

from adapters.base import ClientAdapter


class DashboardAdapter(ClientAdapter):
    """Adapter for the dashboard web chat interface.

    - Uses hook-based permission gating (user clicks Allow/Deny in dashboard)
    - Serves files from proxy via token URLs
    - Supports mid-session mode/model changes via control requests
    - Emits rich events: permission_prompt, plan_mode, system (compacting)
    """

    name = "dashboard"
    serves_file_downloads = True

    def build_client_context(self, mcp_config_path: Path | None) -> str:
        """Build dashboard-specific context.

        MCP tool descriptions are now loaded via the skill system in
        build_agent_prompt() — this method only provides base dashboard context.
        """
        return (
            "You are running in a web dashboard chat interface. "
            "The user is interacting with you via a web browser.\n\n"
            "## Interactive Questions\n"
            "When you need to ask the user a question (especially one with "
            "specific options or choices), you MUST use the `AskUserQuestion` "
            "tool instead of asking in plain text. The dashboard renders "
            "AskUserQuestion as an interactive selection UI where the user "
            "can click to select options. Plain text questions do not get "
            "this interactive treatment. Always prefer AskUserQuestion for "
            "any question that has discrete options or choices."
        )

    async def handle_file_display(
        self,
        session_id: str,
        file_path: Path,
        filename: str,
        description: str,
        download_url: str = "",
    ) -> dict:
        """Shape the file event around the hook-minted download URL."""
        return {
            "event_type": "file",
            "filename": filename,
            "download_url": download_url,
            "description": description,
        }
