"""Base adapter class for client-specific proxy behavior.

Each frontend (phone server, dashboard, etc.) has an adapter that defines
how the proxy interacts with that specific client.  The core proxy is
transport-agnostic — adapters provide the client-specific pieces:
  - System prompt context (display tool instructions, client description)
  - File display delivery (serve from proxy via token URLs)
"""

from abc import ABC, abstractmethod
from pathlib import Path


class ClientAdapter(ABC):
    """Abstract base for client adapters."""

    name: str  # "phone", "dashboard", etc.

    # True when this client renders download buttons — the file hook then
    # mints a durable download token and passes its URL to
    # ``handle_file_display`` (minting lives in the hook, which holds the
    # chat + security context adapters don't have).
    serves_file_downloads: bool = False

    @abstractmethod
    def build_client_context(self, mcp_config_path: Path | None) -> str:
        """Return text to append to the system prompt for this client type.

        Called during session creation / command building.  Should include
        display tool instructions if the agent has the display MCP.
        Return empty string for no additional context.
        """
        ...

    @abstractmethod
    async def handle_file_display(
        self,
        session_id: str,
        file_path: Path,
        filename: str,
        description: str,
        download_url: str = "",
    ) -> dict:
        """Handle a send_file event from the display MCP.

        ``download_url`` is the hook-minted token URL (empty unless
        ``serves_file_downloads``). Returns a dict to push into the session's
        event queue; must include at least ``event_type`` and ``filename``.
        """
        ...
